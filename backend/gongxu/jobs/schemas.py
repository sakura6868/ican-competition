"""Public job progress schemas shared by API routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from gongxu.db.models import Job, JobStatus


class JobOut(BaseModel):
    """Stable, UI-friendly representation of a persisted background job."""

    id: str
    job_type: str
    status: str
    stage: str
    progress_percent: int = Field(ge=0, le=100)
    status_text: str
    error_summary: str | None
    retry_count: int
    max_retries: int
    result: dict[str, Any] | None
    checkpoint: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def job_out(job: Job) -> JobOut:
    checkpoint = dict(job.checkpoint or {})
    if job.status == JobStatus.succeeded:
        progress = 100
    else:
        raw_progress = checkpoint.get("progress_percent", checkpoint.get("progress", 0))
        try:
            progress = (
                int(float(raw_progress) * 100)
                if 0 < float(raw_progress) <= 1
                else int(raw_progress)
            )
        except (TypeError, ValueError):
            progress = 0
    progress = max(0, min(progress, 100))
    default_stage = {
        JobStatus.queued: "queued",
        JobStatus.running: "running",
        JobStatus.retrying: "retrying",
        JobStatus.succeeded: "done",
        JobStatus.failed: "failed",
        JobStatus.cancelled: "cancelled",
    }[job.status]
    stage = str(checkpoint.get("stage") or default_stage)
    status_text = str(checkpoint.get("status_text") or _default_status_text(job.status))
    return JobOut(
        id=job.id,
        job_type=job.job_type,
        status=job.status.value,
        stage=stage,
        progress_percent=progress,
        status_text=status_text,
        error_summary=job.error_summary,
        retry_count=job.retry_count,
        max_retries=job.max_retries,
        result=job.result,
        checkpoint=checkpoint,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _default_status_text(status: JobStatus) -> str:
    return {
        JobStatus.queued: "等待处理",
        JobStatus.running: "正在处理",
        JobStatus.retrying: "等待自动重试",
        JobStatus.succeeded: "处理完成",
        JobStatus.failed: "处理失败",
        JobStatus.cancelled: "任务已取消",
    }[status]
