"""Dedicated leased practice jobs; long offline assessments never share this queue."""

import asyncio
import json
import logging
import time
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.db.models import PracticeObservation, PracticeSession, ProviderReceipt, utcnow
from gongxu.db.session import get_session_factory
from gongxu.jobs import service as jobs
from gongxu.practice import reference, service, verifier
from gongxu.practice.state_machine import apply_verification, reset_confirmation
from gongxu.security.crypto import decrypt_text
from gongxu.sop.schema import SopStep

logger = logging.getLogger(__name__)


def run_once() -> bool:
    with get_session_factory()() as db:
        job = jobs.claim_next(db, worker_name="practice-worker", job_types=["practice_verify"])
        if job is None:
            return False
        obs = db.get(PracticeObservation, job.payload["observation_id"])
        row = db.get(PracticeSession, job.payload["session_id"])
        if not obs or not row:
            jobs.fail(db, job.id, error="练习观察不存在")
            return True
        if obs.status in ("completed", "failed", "discarded"):
            jobs.complete(db, job.id, result={"observation_id": obs.id, "status": obs.status})
            return True
        try:
            if not _current(row, obs):
                _discard(db, obs, job.id)
                return True
            step = next(
                SopStep.model_validate(s)
                for s in row.steps
                if s["index"] == row.state["current_step_index"]
            )
            obs.status = "running"
            db.commit()
            frames = json.loads(decrypt_text(obs.encrypted_frames or ""))
            standards = reference.reference_images(db, step)
            db.commit()  # release the database read transaction before the cloud request
            checks, receipt = verifier.verify_step(step, frames, standards)
            checks = verifier.validate_result({"checks": checks}, step, frames)
            db.expire_all()
            db.refresh(row)
            db.refresh(obs)
            if not _current(row, obs):
                _discard(db, obs, job.id)
                return True
            receipt_row = ProviderReceipt(
                **{
                    k: getattr(receipt, k)
                    for k in (
                        "provider",
                        "mode",
                        "model_id",
                        "prompt_version",
                        "input_digest",
                        "response_digest",
                        "input_tokens",
                        "output_tokens",
                        "latency_ms",
                        "cost_usd",
                        "error",
                    )
                }
            )
            db.add(receipt_row)
            db.flush()
            state, event_type = apply_verification(
                row.state,
                [SopStep.model_validate(s) for s in row.steps],
                checks,
                obs.frames,
                service.now_ms(),
            )
            state["last_observation_id"] = obs.id
            status = "finished" if state["current_step_index"] is None else "active"
            service.save_state(db, row, state, status)
            obs.status = "completed"
            obs.receipt_id = receipt_row.id
            obs.result = {"checks": checks, "latency_ms": receipt.latency_ms}
            service.add_event(db, row, event_type, {"observation_id": obs.id, "result": obs.result})
            if status == "finished":
                service.add_event(db, row, "session_finished")
            db.commit()
            jobs.complete(db, job.id, result={"observation_id": obs.id, "status": obs.status})
        except Exception as exc:
            db.rollback()
            db.refresh(obs)
            db.refresh(row)
            if _current(row, obs):
                state = dict(row.state)
                reset_confirmation(state)
                state.update(
                    pending_observation_id=None,
                    step_state="observing",
                    service_error="本轮核验失败，请稍后重新检查。进度已保留。",
                )
                try:
                    service.save_state(db, row, state)
                    obs.status = "failed"
                    obs.error = type(
                        exc
                    ).__name__  # never expose provider response bodies or credentials
                    obs.encrypted_frames = None
                    service.add_event(db, row, "service_error", {"observation_id": obs.id})
                    db.commit()
                except Exception:
                    db.rollback()
                    logger.exception("Could not persist practice job failure")
            else:
                _discard(db, obs, job.id)
                return True
            jobs.fail(db, job.id, error=f"实时核验失败：{type(exc).__name__}")
        return True


def _current(row: PracticeSession, obs: PracticeObservation) -> bool:
    return (
        row.status == "active"
        and row.allow_provider_export
        and row.state["step_revision"] == obs.step_revision
        and row.state["pending_observation_id"] == obs.id
        and obs.created_at
        >= utcnow() - timedelta(seconds=get_settings().practice_observation_ttl_seconds)
    )


def _discard(db: Session, obs: PracticeObservation, job_id: str) -> None:
    row = db.get(PracticeSession, obs.session_id)
    if row and row.state["pending_observation_id"] == obs.id:
        state = dict(row.state)
        reset_confirmation(state)
        state.update(
            pending_observation_id=None,
            step_state="observing",
            service_error="核验结果已过期，请使用新画面重新检查。",
        )
        service.save_state(db, row, state)
        service.add_event(db, row, "service_error", {"observation_id": obs.id})
    obs.status = "discarded"
    obs.encrypted_frames = None
    db.commit()
    jobs.complete(db, job_id, result={"observation_id": obs.id, "status": "discarded"})


def cleanup_evidence() -> None:
    cutoff = utcnow() - timedelta(days=get_settings().evidence_retention_days)
    with get_session_factory()() as db:
        db.execute(
            update(PracticeObservation)
            .where(PracticeObservation.created_at < cutoff)
            .values(encrypted_frames=None)
        )
        db.commit()


async def serve() -> None:
    await asyncio.to_thread(cleanup_evidence)
    last_cleanup = time.monotonic()
    while True:
        try:
            if time.monotonic() - last_cleanup > 3600:
                await asyncio.to_thread(cleanup_evidence)
                last_cleanup = time.monotonic()
            worked = await asyncio.to_thread(run_once)
            await asyncio.sleep(0.1 if worked else 0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Practice worker failed to poll queue")
            await asyncio.sleep(1)


def main() -> None:
    from gongxu.db.session import init_db

    init_db()
    cleanup_evidence()
    while True:
        if not run_once():
            time.sleep(0.5)


if __name__ == "__main__":
    main()
