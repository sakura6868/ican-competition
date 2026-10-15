"""SQLite 持久化任务队列。

- 幂等键：重复入队返回已有任务，避免重复执行。
- 租约：worker 领取任务后拥有有限期租约，异常退出后可被重新领取。
- 心跳：running 中周期更新，用于检测僵尸任务。
- 阶段检查点：checkpoint 字段支持跨阶段恢复。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from gongxu.db.models import Job, JobStatus, utcnow


class JobAlreadyExistsError(Exception):
    pass


class JobNotFoundError(Exception):
    pass


class JobRetryError(Exception):
    pass


@dataclass(frozen=True)
class _ClaimCandidate:
    """Lease fields observed before an atomic claim attempt."""

    job_id: str
    status: JobStatus
    lease_owner: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None


def enqueue(
    db: Session,
    *,
    job_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    max_retries: int = 3,
) -> Job:
    """入队。幂等键已存在时直接返回已有任务。"""
    existing = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
    if existing is not None:
        return existing
    job = Job(
        job_type=job_type,
        payload=payload,
        idempotency_key=idempotency_key,
        max_retries=max_retries,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _claim_candidates(
    db: Session,
    *,
    now: datetime,
    job_types: list[str] | None,
) -> list[_ClaimCandidate]:
    """Read a bounded set of claimable lease snapshots without ORM identity caching."""
    stmt = select(
        Job.id,
        Job.status,
        Job.lease_owner,
        Job.lease_expires_at,
        Job.heartbeat_at,
    ).where(
        Job.status.in_([JobStatus.queued, JobStatus.retrying, JobStatus.running]),
        or_(Job.lease_expires_at.is_(None), Job.lease_expires_at < now),
    )
    if job_types:
        stmt = stmt.where(Job.job_type.in_(job_types))
    rows = db.execute(stmt.order_by(Job.created_at.asc(), Job.id.asc()).limit(10)).tuples()
    return [
        _ClaimCandidate(
            job_id=job_id,
            status=status,
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
            heartbeat_at=heartbeat_at,
        )
        for job_id, status, lease_owner, lease_expires_at, heartbeat_at in rows
    ]


def _claim_candidate(
    db: Session,
    *,
    candidate: _ClaimCandidate,
    worker_name: str,
    now: datetime,
    lease_seconds: int = 300,
) -> Job | None:
    """Claim one exact lease snapshot with a compare-and-swap UPDATE."""
    owner_matches = (
        Job.lease_owner.is_(None)
        if candidate.lease_owner is None
        else Job.lease_owner == candidate.lease_owner
    )
    lease_matches = (
        Job.lease_expires_at.is_(None)
        if candidate.lease_expires_at is None
        else Job.lease_expires_at == candidate.lease_expires_at
    )
    heartbeat_matches = (
        Job.heartbeat_at.is_(None)
        if candidate.heartbeat_at is None
        else Job.heartbeat_at == candidate.heartbeat_at
    )
    result = cast(
        CursorResult[Any],
        db.execute(
            update(Job)
            .where(
                Job.id == candidate.job_id,
                Job.status == candidate.status,
                owner_matches,
                lease_matches,
                heartbeat_matches,
                or_(Job.lease_expires_at.is_(None), Job.lease_expires_at < now),
            )
            .values(
                status=JobStatus.running,
                lease_owner=worker_name,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
                started_at=func.coalesce(Job.started_at, now),
            )
            .execution_options(synchronize_session=False)
        ),
    )
    if result.rowcount != 1:
        db.rollback()
        return None

    db.commit()
    return db.scalar(
        select(Job)
        .where(Job.id == candidate.job_id)
        .execution_options(populate_existing=True)
    )


def claim_next(db: Session, *, worker_name: str, job_types: list[str] | None = None) -> Job | None:
    """领取下一个可执行任务。

    queued/retrying 且租约过期，或 running 但租约过期（worker 崩溃遗留）均可重领。
    """
    now = utcnow()
    candidates = _claim_candidates(db, now=now, job_types=job_types)
    for candidate in candidates:
        claimed = _claim_candidate(
            db,
            candidate=candidate,
            worker_name=worker_name,
            now=now,
        )
        if claimed is not None:
            return claimed
    return None


def heartbeat(db: Session, job_id: str, *, lease_seconds: int = 300) -> None:
    """worker 运行中更新心跳并续租。"""
    now = utcnow()
    db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
    )
    db.commit()


def save_checkpoint(db: Session, job_id: str, checkpoint: dict[str, Any]) -> None:
    db.execute(update(Job).where(Job.id == job_id).values(checkpoint=checkpoint))
    db.commit()


def set_progress(
    db: Session,
    job_id: str,
    *,
    stage: str,
    progress_percent: int,
    status_text: str,
    details: dict[str, Any] | None = None,
) -> Job:
    """Persist a UI-facing checkpoint while preserving prior stage details."""
    job = db.get(Job, job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    checkpoint = dict(job.checkpoint or {})
    checkpoint.update(details or {})
    checkpoint.update(
        {
            "stage": stage,
            "progress_percent": max(0, min(int(progress_percent), 100)),
            "status_text": status_text,
            "updated_at": utcnow().isoformat(),
        }
    )
    job.checkpoint = checkpoint
    db.commit()
    db.refresh(job)
    return job


def complete(db: Session, job_id: str, *, result: dict[str, Any]) -> Job:
    now = utcnow()
    job = db.get(Job, job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    job.status = JobStatus.succeeded
    job.result = result
    job.error_summary = None
    job.finished_at = now
    job.lease_expires_at = now
    job.lease_owner = None
    db.commit()
    db.refresh(job)
    return job


def fail(db: Session, job_id: str, *, error: str) -> Job:
    """标记失败；未超过 max_retries 时进入 retrying 等待重领。"""
    now = utcnow()
    job = db.get(Job, job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    job.retry_count += 1
    job.error_summary = error[:2000]
    if job.retry_count <= job.max_retries:
        job.status = JobStatus.retrying
        job.lease_owner = None
        job.lease_expires_at = None
        checkpoint = dict(job.checkpoint or {})
        checkpoint.update(
            {
                "resume_stage": checkpoint.get("stage", "running"),
                "stage": "retrying",
                "status_text": f"处理失败，等待第 {job.retry_count} 次自动重试",
            }
        )
        job.checkpoint = checkpoint
    else:
        job.status = JobStatus.failed
        job.finished_at = now
        job.lease_owner = None
        job.lease_expires_at = now
        checkpoint = dict(job.checkpoint or {})
        checkpoint.update({"stage": "failed", "status_text": "处理失败"})
        job.checkpoint = checkpoint
    db.commit()
    db.refresh(job)
    return job


def cancel(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    if job.status in (JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled):
        return job
    job.status = JobStatus.cancelled
    job.finished_at = utcnow()
    job.lease_owner = None
    job.lease_expires_at = job.finished_at
    db.commit()
    db.refresh(job)
    return job


def retry(db: Session, job_id: str) -> Job:
    """Manually requeue a terminal failed job without creating duplicate work."""
    job = db.get(Job, job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    if job.status != JobStatus.failed:
        raise JobRetryError("只有最终失败的任务可以手动重试")
    job.status = JobStatus.queued
    job.retry_count = 0
    job.error_summary = None
    job.lease_owner = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    job.started_at = None
    job.finished_at = None
    job.result = None
    checkpoint = dict(job.checkpoint or {})
    checkpoint.update(
        {
            "stage": "queued",
            "progress_percent": 0,
            "status_text": "已重新加入队列",
            "manual_retry_at": utcnow().isoformat(),
        }
    )
    job.checkpoint = checkpoint
    db.commit()
    db.refresh(job)
    return job


def latest_for_payload(
    db: Session,
    *,
    job_type: str,
    payload_key: str,
    payload_value: str,
) -> Job | None:
    """Find the latest job linked through a JSON payload field.

    Filtering in Python keeps this portable across supported SQLite builds.
    """
    rows = db.scalars(
        select(Job).where(Job.job_type == job_type).order_by(Job.created_at.desc())
    )
    return next(
        (row for row in rows if str((row.payload or {}).get(payload_key, "")) == payload_value),
        None,
    )
