"""Persistent practice lifecycle with optimistic concurrency and scoped observations."""

import json
import time
import uuid
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.db.models import (
    Job,
    PracticeEvent,
    PracticeObservation,
    PracticeSession,
    SopPublishStatus,
    SopVersion,
    SourceAsset,
    User,
    UserRole,
    utcnow,
)
from gongxu.practice.schema import ObservationRequest
from gongxu.practice.state_machine import complete_step, initial_state, reset_confirmation
from gongxu.practice.verifier import normalize_image
from gongxu.security.crypto import encrypt_text
from gongxu.sop.schema import SopStep


def now_ms() -> int:
    return round(time.time() * 1000)


def readiness(row: SopVersion, db: Session) -> list[str]:
    issues = []
    if row.status != SopPublishStatus.published:
        issues.append("课程尚未发布")
    asset = db.get(SourceAsset, row.reference_asset_id) if row.reference_asset_id else None
    if (
        not asset
        or asset.kind != "video"
        or not asset.encrypted_path
        or not Path(asset.encrypted_path).is_file()
        or not asset.allow_provider_export
    ):
        issues.append("标准教学视频未绑定、未保存或未授权视觉分析")
    try:
        steps = [SopStep.model_validate(s) for s in row.steps]
        for step in steps:
            prefix = f"步骤 {step.index}："
            if not step.teaching or step.teaching.source_asset_id != row.reference_asset_id:
                issues.append(prefix + "教学片段未绑定标准视频")
            if not step.verification or not step.verification.teacher_confirmed:
                issues.append(prefix + "核验条件需要教师校对确认")
            elif not any(
                c.required and c.mode == "visual" and c.category == "completion"
                for c in step.verification.checks
            ):
                issues.append(prefix + "缺少可观察的必要完成条件")
        if not steps:
            issues.append("课程没有步骤")
    except ValueError:
        issues.append("课程步骤数据无效")
    return issues


def authorize(db: Session, session_id: str, user: User, *, write: bool = False) -> PracticeSession:
    row = db.get(PracticeSession, session_id)
    if row is None:
        raise HTTPException(404, "练习不存在")
    if user.role == UserRole.observer and write:
        raise HTTPException(403, "观察员只能查看练习")
    if user.id != row.learner_id and user.role not in (
        UserRole.admin,
        UserRole.instructor,
        UserRole.observer,
    ):
        raise HTTPException(403, "无权访问该练习")
    return row


def snapshot(row: PracticeSession) -> dict[str, Any]:
    state = deepcopy(row.state)
    state.setdefault("unknown_streak", 0)
    state.setdefault("step_errors", {})
    state.setdefault("step_unknowns", {})
    return {
        "id": row.id,
        "sop_id": row.sop_id,
        "learner_id": row.learner_id,
        "server_time_ms": now_ms(),
        "status": row.status,
        "revision": row.revision,
        "steps": row.steps,
        "state": state,
        "created_at": row.created_at.isoformat() + "Z",
    }


def learning_report(row: PracticeSession) -> dict[str, Any]:
    """Build an explainable practice report from persisted progression facts."""

    state = snapshot(row)["state"]
    steps = {int(step["index"]): step for step in row.steps}
    attempts = {int(item["step_index"]): item for item in state.get("attempts", [])}
    completed = set(state.get("completed", []))
    skipped = set(state.get("skipped", []))
    detail = []
    for index, step in sorted(steps.items()):
        attempt = attempts.get(index, {})
        duration_ms = int(attempt.get("duration_ms", 0) or 0)
        expected_ms = int(step.get("expected_duration_s", 60) or 60) * 1000
        errors = int(state.get("step_errors", {}).get(str(index), 0) or 0)
        unknowns = int(state.get("step_unknowns", {}).get(str(index), 0) or 0)
        needs_review = errors > 0 or unknowns >= 2 or duration_ms > expected_ms * 1.5
        detail.append(
            {
                "step_index": index,
                "name": step.get("name", f"步骤 {index}"),
                "completed": index in completed,
                "skipped": index in skipped,
                "duration_ms": duration_ms,
                "expected_duration_s": int(step.get("expected_duration_s", 60) or 60),
                "error_count": errors,
                "unknown_count": unknowns,
                "needs_review": needs_review,
            }
        )
    required = [item for item in detail if not steps[item["step_index"]].get("optional", False)]
    completion_ratio = (
        sum(1 for item in required if item["completed"]) / len(required) if required else 0.0
    )
    clean_ratio = (
        sum(1 for item in required if item["completed"] and item["error_count"] == 0)
        / len(required)
        if required
        else 0.0
    )
    practice_index = round((completion_ratio * 0.7 + clean_ratio * 0.3) * 100)
    return {
        "practice_index": practice_index,
        "completion_rate": round(completion_ratio * 100),
        "completed_steps": len(completed),
        "total_steps": len(steps),
        "correction_count": int(state.get("error_count", 0) or 0),
        "call_count": int(state.get("call_count", 0) or 0),
        "weak_steps": [item for item in detail if item["needs_review"]],
        "steps": detail,
        "method_note": "练习指数=必做步骤完成率70%+无纠错完成率30%；仅反映本次系统记录。",
    }


def add_event(
    db: Session, row: PracticeSession, kind: str, detail: dict[str, Any] | None = None
) -> None:
    db.add(
        PracticeEvent(
            session_id=row.id, event_type=kind, payload={"session": snapshot(row), **(detail or {})}
        )
    )


def save_state(
    db: Session, row: PracticeSession, state: dict[str, Any], status: str | None = None
) -> None:
    revision = row.revision
    result = db.execute(
        update(PracticeSession)
        .where(
            PracticeSession.id == row.id,
            PracticeSession.revision == revision,
        )
        .values(
            state=state, status=status or row.status, revision=revision + 1, updated_at=utcnow()
        )
        .execution_options(synchronize_session=False)
    )
    if cast(CursorResult[Any], result).rowcount != 1:
        db.rollback()
        raise HTTPException(409, "练习状态已更新，请刷新后重试")
    db.refresh(row)


def create(db: Session, user: User, sop_id: str, allow_export: bool) -> PracticeSession:
    if user.role == UserRole.observer:
        raise HTTPException(403, "观察员不能开始练习")
    if not allow_export:
        raise HTTPException(400, "请先确认允许将工作区画面发送给视觉模型核验")
    sop = db.get(SopVersion, sop_id)
    if sop is None:
        raise HTTPException(404, "课程不存在")
    issues = readiness(sop, db)
    if issues:
        raise HTTPException(409, "；".join(issues))
    active = db.scalars(
        select(PracticeSession.id).where(
            PracticeSession.learner_id == user.id,
            PracticeSession.status.in_(["active", "paused"]),
        )
    ).all()
    if len(active) >= 5:
        raise HTTPException(409, "请先结束已有练习，再开始新练习")
    row = PracticeSession(
        sop_id=sop.id,
        learner_id=user.id,
        steps=deepcopy(sop.steps),
        status="active",
        revision=0,
        allow_provider_export=True,
        state=initial_state([SopStep.model_validate(s) for s in sop.steps], now_ms()),
    )
    db.add(row)
    db.flush()
    add_event(db, row, "session_created")
    db.commit()
    db.refresh(row)
    return row


def expire_pending(db: Session, row: PracticeSession) -> None:
    pending_id = row.state["pending_observation_id"]
    if not pending_id:
        return
    obs = db.get(PracticeObservation, pending_id)
    if obs and obs.created_at >= utcnow() - timedelta(
        seconds=get_settings().practice_observation_ttl_seconds
    ):
        return
    state = deepcopy(row.state)
    reset_confirmation(state)
    state.update(
        pending_observation_id=None,
        step_state="observing",
        service_error="本轮核验已超时，请使用新画面重新检查。进度已保留。",
    )
    save_state(db, row, state)
    if obs:
        obs.status = "discarded"
        obs.encrypted_frames = None
    add_event(db, row, "service_error", {"observation_id": pending_id})
    db.commit()


def submit(db: Session, row: PracticeSession, request: ObservationRequest) -> PracticeObservation:
    existing = db.scalar(
        select(PracticeObservation).where(
            PracticeObservation.session_id == row.id,
            PracticeObservation.request_key == request.request_key,
        )
    )
    if existing:
        return existing
    if row.status != "active" or request.step_revision != row.state["step_revision"]:
        raise HTTPException(409, "练习已暂停或当前步骤已改变")
    settings = get_settings()
    if not row.allow_provider_export:
        raise HTTPException(403, "本练习没有画面外发授权")
    if not (
        settings.llm_mode == "live"
        and settings.llm_api_key
        and settings.llm_base_url
        and settings.llm_model
    ):
        raise HTTPException(503, "请在后端配置真实视觉 API 后进行摄像头核验")
    state = deepcopy(row.state)
    pending_id = state["pending_observation_id"]
    if pending_id:
        pending = db.get(PracticeObservation, pending_id)
        if pending and pending.created_at > utcnow() - timedelta(
            seconds=settings.practice_observation_ttl_seconds
        ):
            raise HTTPException(409, "上一轮核验仍在处理，请等待结果")
        if pending:
            pending.status = "discarded"
            pending.encrypted_frames = None
        reset_confirmation(state)
    if state["call_count"] >= settings.practice_max_calls:
        raise HTTPException(429, "本次练习已达到核验次数上限，请结束练习")
    clock = now_ms()
    metadata: list[dict[str, Any]] = []
    payload: list[dict[str, Any]] = []
    previous_sequence = state["last_sequence"]
    previous_time = 0
    try:
        for frame in request.frames:
            if frame.sequence <= previous_sequence or frame.captured_at_ms <= previous_time:
                raise ValueError("画面必须是新的、按顺序采集的帧")
            if (
                not max(state["entered_at_ms"], clock - 15_000)
                <= frame.captured_at_ms
                <= clock + 5000
            ):
                raise ValueError("画面过期或属于上一个步骤，请重新采集")
            uri, digest = normalize_image(frame.data_uri)
            item = {
                "id": f"frame_{frame.sequence}",
                "sequence": frame.sequence,
                "captured_at_ms": frame.captured_at_ms,
                "sha256": digest,
            }
            metadata.append(item)
            payload.append({**item, "data_uri": uri})
            previous_sequence, previous_time = frame.sequence, frame.captured_at_ms
        if metadata[-1]["captured_at_ms"] - metadata[0]["captured_at_ms"] < 1000:
            raise ValueError("多帧核验需要至少覆盖 1 秒，请继续保持操作结果可见")
        if len({item["sha256"] for item in metadata}) < 2:
            raise ValueError("多帧画面没有变化，请调整视角后重新采集")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    observation = PracticeObservation(
        id=uuid.uuid4().hex,
        session_id=row.id,
        request_key=request.request_key,
        step_revision=request.step_revision,
        frames=metadata,
        encrypted_frames=encrypt_text(json.dumps(payload)),
        status="queued",
    )
    db.add(observation)
    state.update(
        pending_observation_id=observation.id,
        last_sequence=previous_sequence,
        call_count=state["call_count"] + 1,
        service_error="",
    )
    try:
        save_state(db, row, state)
    except (HTTPException, IntegrityError) as exc:
        # A simultaneous identical request may have committed during image validation.
        db.rollback()
        repeated = db.scalar(
            select(PracticeObservation).where(
                PracticeObservation.session_id == row.id,
                PracticeObservation.request_key == request.request_key,
            )
        )
        if repeated is not None:
            return repeated
        raise HTTPException(409, "练习状态已更新，请使用新画面重试") from exc
    db.add(
        Job(
            job_type="practice_verify",
            idempotency_key=f"practice:{observation.id}",
            payload={
                "observation_id": observation.id,
                "session_id": row.id,
                "requested_by": row.learner_id,
            },
            max_retries=0,
        )
    )
    add_event(db, row, "verification_started", {"observation_id": observation.id})
    db.commit()
    db.refresh(observation)
    return observation


def action(
    db: Session, row: PracticeSession, user: User, name: str, checks: list[str], reason: str
) -> PracticeSession:
    if row.status in ("finished", "stopped"):
        raise HTTPException(409, "本次练习已结束")
    state = deepcopy(row.state)
    status = row.status
    if name == "confirm_manual":
        if user.role not in (UserRole.admin, UserRole.instructor):
            raise HTTPException(403, "人工核验条件需要教师确认")
        if not reason.strip():
            raise HTTPException(422, "请记录人工核验依据")
        step = next(
            SopStep.model_validate(s)
            for s in row.steps
            if s["index"] == state["current_step_index"]
        )
        assert step.verification is not None
        allowed = {c.id for c in step.verification.checks if c.mode == "manual"}
        if not checks or not set(checks) <= allowed:
            raise HTTPException(422, "人工核验条件无效")
        state["manual_checks"] = sorted(set(state["manual_checks"]) | set(checks))
    elif name == "skip_optional":
        if row.status != "active":
            raise HTTPException(409, "请先恢复练习")
        steps = [SopStep.model_validate(s) for s in row.steps]
        step = next(s for s in steps if s.index == state["current_step_index"])
        if not step.optional:
            raise HTTPException(403, "必要步骤不能跳过")
        state = complete_step(state, steps, now_ms(), skipped=True)
        status = "finished" if state["current_step_index"] is None else "active"
    else:
        if name == "resume" and row.status != "paused":
            raise HTTPException(409, "当前练习没有暂停")
        if name == "pause" and row.status != "active":
            raise HTTPException(409, "当前练习无法暂停")
        if row.status == "active":
            state["step_active_ms"] = state.get("step_active_ms", 0) + max(
                0, now_ms() - state["entered_at_ms"]
            )
        status = {"pause": "paused", "resume": "active", "stop": "stopped"}[name]
        state.update(
            step_revision=state["step_revision"] + 1,
            entered_at_ms=now_ms(),
            pending_observation_id=None,
            checks=[],
            step_state="observing",
            service_error="",
        )
    reset_confirmation(state)
    if name == "confirm_manual":
        # Any in-flight observation predates this authorization event.
        if row.status == "active":
            state["step_active_ms"] = state.get("step_active_ms", 0) + max(
                0, now_ms() - state["entered_at_ms"]
            )
        state.update(
            step_revision=state["step_revision"] + 1,
            pending_observation_id=None,
            entered_at_ms=now_ms(),
        )
    save_state(db, row, state, status)
    add_event(db, row, "session_action", {"action": name, "actor_id": user.id, "reason": reason})
    db.commit()
    return row
