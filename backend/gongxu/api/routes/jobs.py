"""Authenticated background-job progress and retry endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from gongxu.assessment import service as assessment_service
from gongxu.db.models import AssessmentSession, Job, SourceAsset, User, UserRole
from gongxu.db.session import get_db
from gongxu.jobs import service
from gongxu.jobs.schemas import JobOut, job_out
from gongxu.security import auth
from gongxu.security.csrf import require_csrf

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _authorize_job(db: Session, user: User, job: Job, *, write: bool = False) -> None:
    if user.role == UserRole.admin:
        return
    if write and user.role == UserRole.observer:
        raise HTTPException(status_code=403, detail="observer 只能读取任务")
    if user.role in (UserRole.instructor, UserRole.observer):
        return

    payload = job.payload or {}
    if payload.get("requested_by") == user.id:
        return
    if job.job_type == "recorded_assessment":
        session_id = payload.get("session_id")
        session = db.get(AssessmentSession, session_id) if session_id else None
        if session is not None:
            try:
                assessment_service.authorize_assessment_session(
                    user,
                    session,
                    "run" if write else "read",
                )
                return
            except assessment_service.ForbiddenError:
                pass
    if job.job_type == "asset_process":
        asset_id = payload.get("asset_id")
        asset = db.get(SourceAsset, asset_id) if asset_id else None
        if asset is not None and asset.authorized_by == user.id:
            return
    raise HTTPException(status_code=403, detail="无权访问该任务")
@router.get("/{job_id}", response_model=JobOut)
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> JobOut:
    row = db.get(Job, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    _authorize_job(db, current_user, row)
    return job_out(row)


@router.post(
    "/{job_id}/retry",
    response_model=JobOut,
    dependencies=[Depends(require_csrf)],
)
def retry_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> JobOut:
    row = db.get(Job, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    _authorize_job(db, current_user, row, write=True)
    try:
        row = service.retry(db, job_id)
    except service.JobRetryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if row.job_type == "recorded_assessment":
        session_id = (row.payload or {}).get("session_id")
        session = db.get(AssessmentSession, session_id) if session_id else None
        if session is not None:
            from gongxu.db.models import AssessmentStatus

            session.status = AssessmentStatus.processing
            session.finished_at = None
            db.commit()
    return job_out(row)
