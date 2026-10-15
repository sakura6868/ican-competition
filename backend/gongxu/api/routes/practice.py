import asyncio
import base64
import json
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, Response, StreamingResponse

from gongxu.assets.service import decrypt_to_temp
from gongxu.config import get_settings
from gongxu.db.models import (
    PracticeEvent,
    PracticeObservation,
    PracticeSession,
    ProviderReceipt,
    SopPublishStatus,
    SopVersion,
    User,
    UserRole,
)
from gongxu.db.session import get_db, get_session_factory
from gongxu.practice import reference, service
from gongxu.practice.schema import ObservationRequest, PracticeAction, PracticeCreate
from gongxu.security import auth
from gongxu.security.crypto import decrypt_text
from gongxu.security.csrf import require_csrf
from gongxu.sop.schema import SopStep

router = APIRouter(prefix="/practice-sessions", tags=["practice"])


@router.get("/courses")
def courses(
    db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)
) -> dict[str, Any]:
    settings = get_settings()
    rows = db.scalars(
        select(SopVersion)
        .where(SopVersion.status == SopPublishStatus.published)
        .order_by(SopVersion.process_key, SopVersion.version.desc(), SopVersion.created_at.desc())
    ).all()
    latest: dict[str, SopVersion] = {}
    for row in rows:
        latest.setdefault(row.process_key, row)
    return {
        "provider_configured": bool(
            settings.llm_mode == "live"
            and settings.llm_api_key
            and settings.llm_base_url
            and settings.llm_model
        ),
        "courses": [
            {
                "sop_id": r.id,
                "process_key": r.process_key,
                "version": r.version,
                "course_meta": dict(r.course_meta or {}),
                "step_count": len(r.steps),
                "issues": service.readiness(r, db),
            }
            for r in latest.values()
        ],
    }


@router.get("")
def list_sessions(
    db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)
) -> list[dict[str, Any]]:
    stmt = select(PracticeSession)
    if user.role not in (UserRole.admin, UserRole.instructor, UserRole.observer):
        stmt = stmt.where(PracticeSession.learner_id == user.id)
    rows = db.scalars(stmt.order_by(PracticeSession.created_at.desc()).limit(100)).all()
    learners = (
        {
            item.id: item
            for item in db.scalars(
                select(User).where(User.id.in_({row.learner_id for row in rows}))
            ).all()
        }
        if rows
        else {}
    )
    courses_by_id = (
        {
            item.id: item
            for item in db.scalars(
                select(SopVersion).where(SopVersion.id.in_({row.sop_id for row in rows}))
            ).all()
        }
        if rows
        else {}
    )
    output = []
    for row in rows:
        item = service.snapshot(row)
        learner = learners.get(row.learner_id)
        course = courses_by_id.get(row.sop_id)
        item["learner"] = {
            "id": row.learner_id,
            "display_name": (learner.display_name or learner.username) if learner else "未知学员",
        }
        item["course"] = {
            "process_key": course.process_key if course else "",
            "course_meta": dict(course.course_meta or {}) if course else {},
        }
        item["report"] = service.learning_report(row)
        output.append(item)
    return output


@router.get("/analytics")
def analytics(
    db: Session = Depends(get_db),
    user: User = Depends(
        auth.require_roles(UserRole.admin, UserRole.instructor, UserRole.observer)
    ),
) -> dict[str, Any]:
    rows = db.scalars(select(PracticeSession).order_by(PracticeSession.created_at.desc())).all()
    learners = (
        {
            item.id: item
            for item in db.scalars(
                select(User).where(User.id.in_({row.learner_id for row in rows}))
            ).all()
        }
        if rows
        else {}
    )
    courses_by_id = (
        {
            item.id: item
            for item in db.scalars(
                select(SopVersion).where(SopVersion.id.in_({row.sop_id for row in rows}))
            ).all()
        }
        if rows
        else {}
    )
    observations = (
        db.scalars(
            select(PracticeObservation).where(
                PracticeObservation.session_id.in_({row.id for row in rows}),
                PracticeObservation.receipt_id.is_not(None),
            )
        ).all()
        if rows
        else []
    )
    receipt_ids = {item.receipt_id for item in observations if item.receipt_id}
    receipts = (
        {
            item.id: item
            for item in db.scalars(
                select(ProviderReceipt).where(ProviderReceipt.id.in_(receipt_ids))
            ).all()
        }
        if receipt_ids
        else {}
    )
    latency_by_session: dict[str, list[int]] = {}
    for observation in observations:
        receipt = receipts.get(observation.receipt_id or "")
        if receipt:
            latency_by_session.setdefault(observation.session_id, []).append(receipt.latency_ms)
    sessions: list[dict[str, Any]] = []
    weak_totals: dict[tuple[str, int, str], dict[str, Any]] = {}
    for row in rows:
        report = service.learning_report(row)
        learner = learners.get(row.learner_id)
        course = courses_by_id.get(row.sop_id)
        title = (course.course_meta or {}).get("title") if course else ""
        title = title or (course.process_key if course else "未命名课程")
        for step in report["weak_steps"]:
            key = (row.sop_id, step["step_index"], step["name"])
            total = weak_totals.setdefault(
                key,
                {
                    "sop_id": row.sop_id,
                    "course_title": title,
                    "step_index": step["step_index"],
                    "name": step["name"],
                    "learners": set(),
                    "corrections": 0,
                    "unknowns": 0,
                },
            )
            total["learners"].add(row.learner_id)
            total["corrections"] += step["error_count"]
            total["unknowns"] += step["unknown_count"]
        latencies = latency_by_session.get(row.id, [])
        sessions.append(
            {
                "id": row.id,
                "learner_id": row.learner_id,
                "learner_name": (learner.display_name or learner.username)
                if learner
                else "未知学员",
                "sop_id": row.sop_id,
                "course_title": title,
                "status": row.status,
                "created_at": row.created_at.isoformat() + "Z",
                "average_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
                "report": report,
            }
        )
    weak_steps: list[dict[str, Any]] = []
    for value in weak_totals.values():
        learners_for_step = value["learners"]
        weak_steps.append(
            {key: item for key, item in value.items() if key != "learners"}
            | {"learner_count": len(learners_for_step)}
        )
    finished = [item for item in sessions if item["status"] == "finished"]
    return {
        "summary": {
            "learner_count": len({row.learner_id for row in rows}),
            "session_count": len(rows),
            "finished_count": len(finished),
            "completion_rate": round(len(finished) / len(rows) * 100) if rows else 0,
            "correction_count": sum(item["report"]["correction_count"] for item in sessions),
            "provider_call_count": sum(item["report"]["call_count"] for item in sessions),
        },
        "weak_steps": sorted(
            weak_steps, key=lambda item: (item["learner_count"], item["corrections"]), reverse=True
        ),
        "sessions": sessions,
        "method_note": "统计来自系统练习记录；完成率与练习指数不等同于教学效果或真实掌握度。",
    }


@router.get("/{session_id}/report")
def session_report(
    session_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)
) -> dict[str, Any]:
    row = service.authorize(db, session_id, user)
    return service.learning_report(row)


@router.post("", dependencies=[Depends(require_csrf)])
def create_session(
    body: PracticeCreate, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)
) -> dict[str, Any]:
    return service.snapshot(service.create(db, user, body.sop_id, body.allow_provider_export))


@router.get("/{session_id}")
def get_session(
    session_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)
) -> dict[str, Any]:
    row = service.authorize(db, session_id, user)
    service.expire_pending(db, row)
    return service.snapshot(row)


@router.post("/{session_id}/observations", status_code=202, dependencies=[Depends(require_csrf)])
def observe(
    session_id: str,
    body: ObservationRequest,
    db: Session = Depends(get_db),
    user: User = Depends(auth.get_current_user),
) -> dict[str, str]:
    row = service.authorize(db, session_id, user, write=True)
    obs = service.submit(db, row, body)
    return {"observation_id": obs.id, "status": obs.status}


@router.post("/{session_id}/actions", dependencies=[Depends(require_csrf)])
def act(
    session_id: str,
    body: PracticeAction,
    db: Session = Depends(get_db),
    user: User = Depends(auth.get_current_user),
) -> dict[str, Any]:
    row = service.authorize(db, session_id, user, write=True)
    return service.snapshot(service.action(db, row, user, body.action, body.check_ids, body.reason))


def _events_since(session_id: str, after: int) -> list[dict[str, Any]]:
    with get_session_factory()() as db:
        events = db.scalars(
            select(PracticeEvent)
            .where(
                PracticeEvent.session_id == session_id,
                PracticeEvent.id > after,
            )
            .order_by(PracticeEvent.id)
            .limit(100)
        ).all()
        return [{"id": e.id, "type": e.event_type, "payload": e.payload} for e in events]


@router.get("/{session_id}/events")
async def events(
    session_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(auth.get_current_user),
) -> StreamingResponse:
    service.authorize(db, session_id, user)
    try:
        cursor = max(after, int(last_event_id or 0))
    except ValueError as exc:
        raise HTTPException(422, "事件游标无效") from exc

    async def stream() -> AsyncIterator[str]:
        nonlocal cursor
        while not await request.is_disconnected():
            batch = await asyncio.to_thread(_events_since, session_id, cursor)
            for event in batch:
                cursor = event["id"]
                yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if not batch:
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/{session_id}/teaching-video")
def teaching_video(
    session_id: str, db: Session = Depends(get_db), user: User = Depends(auth.get_current_user)
) -> FileResponse:
    row = service.authorize(db, session_id, user)
    asset_id = row.steps[0]["teaching"]["source_asset_id"]
    root = get_settings().data_root / "runtime" / "previews"
    root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="practice_", dir=root))
    try:
        path = decrypt_to_temp(db, asset_id, temp_dir=temporary)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"Cache-Control": "private, no-store"},
        background=BackgroundTask(shutil.rmtree, temporary, ignore_errors=True),
    )


@router.get("/{session_id}/reference")
def standard_image(
    session_id: str,
    step_index: int = Query(ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(auth.get_current_user),
) -> Response:
    row = service.authorize(db, session_id, user)
    step = next((SopStep.model_validate(s) for s in row.steps if s["index"] == step_index), None)
    if step is None:
        raise HTTPException(404, "步骤不存在")
    images = reference.reference_images(db, step)
    return Response(
        base64.b64decode(images[-1].split(",", 1)[1]),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/{session_id}/observations/{observation_id}/frames/{frame_id}")
def evidence_frame(
    session_id: str,
    observation_id: str,
    frame_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(auth.get_current_user),
) -> Response:
    service.authorize(db, session_id, user)
    obs = db.get(PracticeObservation, observation_id)
    if not obs or obs.session_id != session_id or not obs.encrypted_frames:
        raise HTTPException(404, "画面证据不存在或已过保留期")
    frame = next(
        (f for f in json.loads(decrypt_text(obs.encrypted_frames)) if f["id"] == frame_id), None
    )
    if frame is None:
        raise HTTPException(404, "画面不存在")
    return Response(
        base64.b64decode(frame["data_uri"].split(",", 1)[1]),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, no-store"},
    )
