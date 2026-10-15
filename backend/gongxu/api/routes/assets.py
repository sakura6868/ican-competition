"""资产路由：上传、登记、列表。

上传处理：临时保存 → SHA256 → 加密存储 → 删除明文临时文件。
权限：admin/instructor 可管理全部资产；learner 仅上传、读取自己的考评视频。
"""

from __future__ import annotations

import mimetypes
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from gongxu.assets import service
from gongxu.config import get_settings
from gongxu.db.models import (
    AuditAction,
    DocumentChunk,
    Job,
    JobStatus,
    SourceAsset,
    User,
    UserRole,
)
from gongxu.db.session import get_db
from gongxu.jobs import service as jobs_service
from gongxu.security import auth
from gongxu.security.audit import record_audit
from gongxu.security.csrf import require_csrf

router = APIRouter(prefix="/assets", tags=["assets"])

WRITERS = (UserRole.admin, UserRole.instructor)
READERS = (UserRole.admin, UserRole.instructor, UserRole.observer, UserRole.learner)


class AssetOut(BaseModel):
    id: str
    kind: str
    filename: str
    sha256: str
    license_note: str
    authorized_by: str | None
    has_encrypted_storage: bool
    allow_provider_export: bool
    indexed: bool = False
    processing_job_id: str | None = None
    processing_status: str | None = None
    created_at: object


def _out(
    row: SourceAsset,
    *,
    indexed: bool = False,
    processing_job: Job | None = None,
) -> AssetOut:
    return AssetOut(
        id=row.id,
        kind=row.kind,
        filename=row.filename,
        sha256=row.sha256,
        license_note=row.license_note,
        authorized_by=row.authorized_by,
        has_encrypted_storage=bool(row.encrypted_path),
        allow_provider_export=row.allow_provider_export,
        indexed=indexed,
        processing_job_id=processing_job.id if processing_job else None,
        processing_status=processing_job.status.value if processing_job else None,
        created_at=row.created_at,
    )


def _can_manage_asset(user: User, asset: SourceAsset) -> bool:
    if user.role in WRITERS:
        return True
    return (
        user.role == UserRole.learner and asset.authorized_by == user.id and asset.kind == "video"
    )


def _require_asset_access(db: Session, user: User, asset_id: str) -> SourceAsset:
    asset = db.get(SourceAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    if user.role == UserRole.learner and asset.authorized_by != user.id:
        raise HTTPException(status_code=403, detail="无权访问其他学员的视频")
    if user.role not in READERS:
        raise HTTPException(status_code=403, detail="权限不足")
    return asset


@router.get("", response_model=list[AssetOut])
def list_assets(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> list[AssetOut]:
    if current_user.role not in READERS:
        raise HTTPException(status_code=403, detail="权限不足")
    stmt = select(SourceAsset).order_by(SourceAsset.created_at.desc())
    if current_user.role == UserRole.learner:
        stmt = stmt.where(
            SourceAsset.authorized_by == current_user.id,
            SourceAsset.kind == "video",
        )
    assets = list(db.scalars(stmt))
    indexed_ids = set(db.scalars(select(DocumentChunk.asset_id).distinct()).all())
    latest_jobs: dict[str, Job] = {}
    for job in (
        db.query(Job).filter(Job.job_type == "asset_process").order_by(Job.created_at.desc()).all()
    ):
        asset_id = str((job.payload or {}).get("asset_id") or "")
        if asset_id and asset_id not in latest_jobs:
            latest_jobs[asset_id] = job
    return [
        _out(
            asset,
            indexed=asset.id in indexed_ids,
            processing_job=latest_jobs.get(asset.id),
        )
        for asset in assets
    ]


@router.post(
    "",
    response_model=AssetOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
async def upload_asset(
    file: UploadFile,
    kind: str = Form(""),
    license_note: str = Form(""),
    allow_provider_export: bool = Form(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> AssetOut:
    if current_user.role == UserRole.learner and kind != "video":
        raise HTTPException(status_code=403, detail="学员只能上传自己的考评视频")
    if current_user.role not in (*WRITERS, UserRole.learner):
        raise HTTPException(status_code=403, detail="权限不足")
    try:
        asset = await service.register_uploaded_asset(
            db,
            upload=file,
            kind=kind,
            license_note=license_note,
            authorized_by=current_user.id,
            allow_provider_export=allow_provider_export,
        )
    except service.AssetTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except service.AssetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit(
        db,
        action=AuditAction.asset_upload,
        actor_id=current_user.id,
        target_type="asset",
        target_id=asset.id,
        detail={"kind": asset.kind, "filename": asset.filename, "sha256": asset.sha256[:16]},
    )
    return _out(asset)


class ProcessResponse(BaseModel):
    job_id: str
    status: str


class UploadAndProcessResponse(BaseModel):
    asset: AssetOut
    job_id: str
    job_status: str


@router.post(
    "/upload-and-process",
    response_model=UploadAndProcessResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
async def upload_and_process(
    file: UploadFile,
    kind: str = Form(""),
    license_note: str = Form(""),
    allow_provider_export: bool = Form(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> UploadAndProcessResponse:
    asset_out = await upload_asset(
        file=file,
        kind=kind,
        license_note=license_note,
        allow_provider_export=allow_provider_export,
        db=db,
        current_user=current_user,
    )
    job = jobs_service.enqueue(
        db,
        job_type="asset_process",
        payload={"asset_id": asset_out.id, "requested_by": current_user.id},
        idempotency_key=f"process:{asset_out.id}",
        max_retries=3,
    )
    return UploadAndProcessResponse(
        asset=asset_out,
        job_id=job.id,
        job_status=job.status.value,
    )


@router.post(
    "/{asset_id}/process", response_model=ProcessResponse, dependencies=[Depends(require_csrf)]
)
def process_asset(
    asset_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> ProcessResponse:
    """入队资产处理任务；最终失败时恢复原任务，不创建新的幂等键。"""
    asset = db.get(SourceAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    if not _can_manage_asset(current_user, asset):
        raise HTTPException(status_code=403, detail="无权处理该资产")
    job = jobs_service.enqueue(
        db,
        job_type="asset_process",
        payload={"asset_id": asset_id, "requested_by": current_user.id},
        idempotency_key=f"process:{asset_id}",
        max_retries=3,
    )
    if job.status == JobStatus.failed:
        job = jobs_service.retry(db, job.id)
    return ProcessResponse(job_id=job.id, status=job.status.value)


@router.get("/{asset_id}/preview")
def preview_asset(
    asset_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> FileResponse:
    """Decrypt a local asset for authenticated, seekable browser playback."""
    asset = _require_asset_access(db, current_user, asset_id)
    if asset.kind not in ("video", "image"):
        raise HTTPException(status_code=400, detail="该资产类型不支持媒体预览")

    settings = get_settings()
    preview_root = settings.data_root / "runtime" / "previews"
    preview_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f"preview_{asset.id}_", dir=preview_root))
    try:
        plain = service.decrypt_to_temp(db, asset.id, temp_dir=temp_dir)
    except service.AssetNotFoundError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=404, detail="资产文件不存在") from exc
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    media_type = mimetypes.guess_type(asset.filename)[0]
    if asset.kind == "video" and not media_type:
        media_type = "video/mp4"
    return FileResponse(
        path=plain,
        media_type=media_type,
        filename=asset.filename,
        content_disposition_type="inline",
        background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
        headers={"Cache-Control": "private, no-store"},
    )
