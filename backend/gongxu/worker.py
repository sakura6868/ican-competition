"""后台 worker：领取并执行任务队列中的作业。

用法：uv run python -m gongxu.worker
"""

from __future__ import annotations

import argparse
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from gongxu.db.models import AssessmentSession, AssessmentStatus, Job, SopVersion
from gongxu.db.session import get_session_factory, init_db
from gongxu.jobs import service as jobs
from gongxu.media.processor import ProcessError, run_asset_job

_JOB_TYPES = {"asset_process", "recorded_assessment", "sop_compile"}
_HEARTBEAT_EVERY_SECONDS = 30.0


def _canonical_source_asset_set(value: object) -> frozenset[str] | None:
    """Normalize a persisted source list; malformed provenance never matches."""
    if not isinstance(value, list):
        return None
    normalized: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        normalized.add(item)
    return frozenset(normalized)


@contextmanager
def _lease_heartbeat(job_id: str) -> Iterator[None]:
    """Keep a long blocking media/provider call from losing its worker lease."""
    stop = threading.Event()

    def pulse() -> None:
        factory = get_session_factory()
        while not stop.wait(_HEARTBEAT_EVERY_SECONDS):
            heartbeat_db = factory()
            try:
                jobs.heartbeat(heartbeat_db, job_id)
            except Exception:  # noqa: BLE001  主任务负责最终状态，心跳异常不应终止它
                heartbeat_db.rollback()
            finally:
                heartbeat_db.close()

    thread = threading.Thread(target=pulse, name=f"job-heartbeat-{job_id[:8]}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2.0)


def _claim_and_run(factory: sessionmaker[Session]) -> bool:
    db = factory()
    try:
        job = jobs.claim_next(db, worker_name="local-worker", job_types=list(_JOB_TYPES))
        if job is None:
            return False
        try:
            if job.job_type == "asset_process":
                jobs.set_progress(
                    db,
                    job.id,
                    stage="processing_asset",
                    progress_percent=10,
                    status_text="正在处理上传资产",
                )
                with _lease_heartbeat(job.id):
                    run_asset_job(db, job.id)
            elif job.job_type == "recorded_assessment":
                _run_recorded_job(db, job)
            elif job.job_type == "sop_compile":
                _run_sop_compile_job(db, job)
            else:
                jobs.fail(db, job.id, error=f"未注册的任务类型: {job.job_type}")
        except ProcessError as exc:
            failed = jobs.fail(db, job.id, error=str(exc))
            _sync_assessment_failure(db, job, failed.status.value)
        except Exception as exc:  # noqa: BLE001  worker 必须隔离单任务异常
            failed = jobs.fail(db, job.id, error=f"{type(exc).__name__}: {exc}")
            _sync_assessment_failure(db, job, failed.status.value)
        return True
    finally:
        db.close()


def _run_recorded_job(db: Session, job: Job) -> None:
    """执行预录考评任务并更新 job 状态/检查点。"""
    import tempfile

    from gongxu.assessment.service import run_recorded_assessment_job
    from gongxu.config import get_settings

    settings = get_settings()
    work_root = settings.data_root / "runtime" / "work"
    work_root.mkdir(parents=True, exist_ok=True)

    def report_progress(
        stage: str,
        progress_percent: int,
        status_text: str,
        details: dict[str, Any] | None,
    ) -> None:
        jobs.heartbeat(db, job.id)
        jobs.set_progress(
            db,
            job.id,
            stage=stage,
            progress_percent=progress_percent,
            status_text=status_text,
            details=details,
        )

    with tempfile.TemporaryDirectory(prefix=f"{job.id}_", dir=work_root) as tmpdir:
        jobs.heartbeat(db, job.id)
        with _lease_heartbeat(job.id):
            result = run_recorded_assessment_job(
                db,
                job.id,
                work_dir=Path(tmpdir),
                progress_callback=report_progress,
            )
        jobs.set_progress(
            db,
            job.id,
            stage="done",
            progress_percent=100,
            status_text="考评任务已完成",
            details=result,
        )
        jobs.complete(db, job.id, result=result)


def _run_sop_compile_job(db: Session, job: Job) -> None:
    """Compile a standard video into a recoverable SOP draft job."""
    from gongxu.providers.factory import get_provider
    from gongxu.sop.service import compile_sop_from_media

    payload = job.payload or {}
    process_key = str(payload.get("process_key") or "")
    video_asset_id = str(payload.get("reference_video_asset_id") or "")
    raw_manual_asset_ids = payload.get("manual_asset_ids", [])
    if not isinstance(raw_manual_asset_ids, list):
        raise ValueError("SOP 编译任务的 manual_asset_ids 必须是列表")
    manual_asset_ids: list[str] = []
    for item in raw_manual_asset_ids:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("SOP 编译任务包含无效的手册资产 ID")
        manual_asset_ids.append(item)
    requested_by = str(payload.get("requested_by") or "") or None
    if not process_key or not video_asset_id:
        raise ValueError("SOP 编译任务缺少 process_key 或 reference_video_asset_id")
    expected_source_assets = _canonical_source_asset_set([video_asset_id, *manual_asset_ids])
    if expected_source_assets is None:
        raise ValueError("SOP 编译任务包含无效的来源资产 ID")

    # Recover the narrow crash window where the draft committed before Job.complete.
    candidates = (
        db.query(SopVersion)
        .filter(
            SopVersion.process_key == process_key,
            SopVersion.reference_asset_id == video_asset_id,
            SopVersion.compiled_by == requested_by,
            SopVersion.created_at >= job.created_at,
        )
        .order_by(SopVersion.created_at.desc())
        .all()
    )
    existing = next(
        (
            candidate
            for candidate in candidates
            if _canonical_source_asset_set(candidate.source_asset_ids) == expected_source_assets
            and (
                not payload.get("teaching_mode")
                or (
                    candidate.steps
                    and all(s.get("teaching") and s.get("verification") for s in candidate.steps)
                )
            )
        ),
        None,
    )
    if existing is not None:
        result = {
            "sop_id": existing.id,
            "sop_version": existing.version,
            "status": existing.status.value,
            "recovered": True,
        }
        jobs.set_progress(
            db,
            job.id,
            stage="draft_ready",
            progress_percent=100,
            status_text="已恢复先前生成的 SOP 草稿",
            details=result,
        )
        jobs.complete(db, job.id, result=result)
        return

    def report_progress(
        stage: str,
        progress_percent: int,
        status_text: str,
        details: dict[str, Any] | None,
    ) -> None:
        jobs.heartbeat(db, job.id)
        jobs.set_progress(
            db,
            job.id,
            stage=stage,
            progress_percent=progress_percent,
            status_text=status_text,
            details=details,
        )

    with _lease_heartbeat(job.id):
        row = compile_sop_from_media(
            db,
            provider=get_provider(),
            process_key=process_key,
            reference_video_asset_id=video_asset_id,
            manual_asset_ids=manual_asset_ids,
            compiled_by=requested_by,
            progress_callback=report_progress,
            teaching_mode=bool(payload.get("teaching_mode", False)),
        )
    result = {
        "sop_id": row.id,
        "sop_version": row.version,
        "status": row.status.value,
        "recovered": False,
    }
    jobs.set_progress(
        db,
        job.id,
        stage="draft_ready",
        progress_percent=100,
        status_text="SOP 草稿已生成",
        details=result,
    )
    jobs.complete(db, job.id, result=result)


def _sync_assessment_failure(db: Session, job: Job, job_status: str) -> None:
    if job.job_type != "recorded_assessment":
        return
    session_id = (job.payload or {}).get("session_id")
    session = db.get(AssessmentSession, session_id) if session_id else None
    if session is None:
        return
    session.status = (
        AssessmentStatus.failed if job_status == "failed" else AssessmentStatus.processing
    )
    db.commit()


def main() -> int:
    from gongxu.config import get_settings
    from gongxu.runtime_cleanup import cleanup_worker_startup_temps

    parser = argparse.ArgumentParser(description="工序智传任务 worker")
    parser.add_argument("--once", action="store_true", help="只处理一个任务后退出")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()

    # A startup cleanup is deliberately completed before this process can
    # obtain a session or claim any queued work.
    cleanup_worker_startup_temps(get_settings())
    init_db()
    factory = get_session_factory()
    print(f"[worker] listening job_types={sorted(_JOB_TYPES)}")
    while True:
        did_run = _claim_and_run(factory)
        if args.once:
            return 0
        if not did_run:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
