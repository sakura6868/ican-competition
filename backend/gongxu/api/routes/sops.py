"""SOP 路由：编译、发布、查询。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gongxu.db.models import (
    AuditAction,
    SopPublishStatus,
    SopVersion,
    SourceAsset,
    User,
    UserRole,
)
from gongxu.db.session import get_db
from gongxu.jobs import service as jobs_service
from gongxu.providers.base import ProviderError
from gongxu.providers.factory import get_provider
from gongxu.security import auth
from gongxu.security.audit import record_audit
from gongxu.security.csrf import require_csrf
from gongxu.sop import service
from gongxu.sop import vocab as vocab_service
from gongxu.sop.schema import CourseMetadata

router = APIRouter(prefix="/sops", tags=["sops"])

WRITERS = (UserRole.admin, UserRole.instructor)
READERS = (UserRole.admin, UserRole.instructor, UserRole.observer, UserRole.learner)


def require_writer(current_user: User = Depends(auth.get_current_user)) -> User:
    if current_user.role not in WRITERS:
        raise HTTPException(status_code=403, detail="权限不足")
    return current_user


class SopOut(BaseModel):
    id: str
    process_key: str
    version: int
    status: str
    source_grade: str
    steps: list[dict[str, Any]]
    course_meta: dict[str, Any]
    scoring_profile: dict[str, Any]
    prompt_version: str
    provider_receipt_id: str | None
    reference_asset_id: str | None
    source_asset_ids: list[str]
    compiled_by: str | None
    published_at: object | None
    created_at: object


class CompileRequest(BaseModel):
    teaching_mode: bool = False
    process_key: str = Field(min_length=1, max_length=128)
    reference_video_asset_id: str = Field(min_length=1, max_length=32)
    manual_asset_ids: list[str] = Field(default_factory=list, max_length=10)


class PublishRequest(BaseModel):
    pass


class SopPatch(BaseModel):
    steps: list[dict[str, Any]] | None = Field(default=None, max_length=64)
    scoring_profile: dict[str, Any] | None = None
    source_grade: str | None = Field(
        default=None,
        pattern="^(video_reference|manual_backed|sensor_verified)$",
    )
    course_meta: CourseMetadata | None = None


class CompileJobResponse(BaseModel):
    job_id: str
    status: str


class VocabularyOut(BaseModel):
    process_key: str
    tools: list[str]
    objects: list[str]
    actions: list[str]
    approved: bool
    source: str


class VocabularyPut(BaseModel):
    tools: list[str] = Field(default_factory=list, max_length=128)
    objects: list[str] = Field(default_factory=list, max_length=128)
    actions: list[str] = Field(default_factory=list, max_length=128)
    approved: bool = False


def _out(row: SopVersion) -> SopOut:
    return SopOut(
        id=row.id,
        process_key=row.process_key,
        version=row.version,
        status=row.status.value,
        source_grade=row.source_grade.value,
        steps=row.steps,
        course_meta=dict(row.course_meta or {}),
        scoring_profile=row.scoring_profile,
        prompt_version=row.prompt_version,
        provider_receipt_id=row.provider_receipt_id,
        reference_asset_id=row.reference_asset_id,
        source_asset_ids=list(row.source_asset_ids or []),
        compiled_by=row.compiled_by,
        published_at=row.published_at,
        created_at=row.created_at,
    )


def _vocabulary_out(vocabulary: vocab_service.ControlledVocabulary) -> VocabularyOut:
    prompt = vocabulary.as_prompt()
    return VocabularyOut(
        process_key=vocabulary.process_key,
        tools=prompt["tools"],
        objects=prompt["objects"],
        actions=prompt["actions"],
        approved=vocabulary.approved,
        source=vocabulary.source,
    )


@router.get("", response_model=list[SopOut])
def list_sops(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> list[SopOut]:
    if current_user.role not in READERS:
        raise HTTPException(status_code=403, detail="权限不足")
    visible_status = SopPublishStatus.published if current_user.role == UserRole.learner else None
    return [_out(r) for r in service.list_sops(db, status=visible_status)]


@router.get("/vocabularies/{process_key}", response_model=VocabularyOut)
def get_vocabulary(
    process_key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> VocabularyOut:
    if current_user.role not in READERS:
        raise HTTPException(status_code=403, detail="权限不足")
    vocabulary = vocab_service.get_process_vocabulary(
        process_key,
        db=db,
        include_proposed=current_user.role in WRITERS,
    )
    if vocabulary is None:
        raise HTTPException(status_code=404, detail="该工序尚未登记受控词表")
    return _vocabulary_out(vocabulary)


@router.patch(
    "/vocabularies/{process_key}",
    response_model=VocabularyOut,
    dependencies=[Depends(require_csrf)],
)
def put_vocabulary(
    process_key: str,
    body: VocabularyPut,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_writer),
) -> VocabularyOut:
    try:
        vocabulary = vocab_service.save_process_vocabulary(
            db,
            process_key,
            tools=body.tools,
            objects=body.objects,
            actions=body.actions,
            approved=body.approved,
            updated_by=current_user.id,
        )
    except vocab_service.SopSemanticError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _vocabulary_out(vocabulary)


@router.get("/{sop_id}", response_model=SopOut)
def get_sop(
    sop_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> SopOut:
    if current_user.role not in READERS:
        raise HTTPException(status_code=403, detail="权限不足")
    try:
        row = service.get_sop(db, sop_id)
    except service.SopNotFoundError as exc:
        raise HTTPException(status_code=404, detail="SOP 不存在") from exc
    if current_user.role == UserRole.learner and row.status != SopPublishStatus.published:
        raise HTTPException(status_code=404, detail="SOP 不存在")
    return _out(row)


@router.post(
    "/compile-jobs",
    response_model=CompileJobResponse,
    status_code=202,
    dependencies=[Depends(require_csrf)],
)
def create_compile_job(
    body: CompileRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_writer),
) -> CompileJobResponse:
    try:
        # Validate configuration before enqueueing.  Otherwise a replay-mode
        # installation would accept the job and silently compile a fixed SOP.
        get_provider()
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    video = db.get(SourceAsset, body.reference_video_asset_id)
    if video is None or video.kind != "video":
        raise HTTPException(status_code=400, detail="标准视频资产不存在或类型不是视频")
    for asset_id in body.manual_asset_ids:
        manual = db.get(SourceAsset, asset_id)
        if manual is None or manual.kind not in ("manual", "image"):
            raise HTTPException(status_code=400, detail=f"参考资料不存在或类型不支持: {asset_id}")
    manual_key = ",".join(sorted(body.manual_asset_ids))
    job = jobs_service.enqueue(
        db,
        job_type="sop_compile",
        payload={
            **body.model_dump(),
            "requested_by": current_user.id,
        },
        idempotency_key=(
            f"sop-compile:{current_user.id}:{body.process_key}:"
            f"{body.reference_video_asset_id}:{manual_key}:v1"
            + (":teaching" if body.teaching_mode else "")
        ),
        max_retries=3,
    )
    return CompileJobResponse(job_id=job.id, status=job.status.value)


@router.post(
    "/compile", response_model=SopOut, status_code=201, dependencies=[Depends(require_csrf)]
)
def compile_sop(
    body: CompileRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_writer),
) -> SopOut:
    """从授权标准视频 + 手册证据编译 SOP 草稿（整改 P1-A：不接受任意 analysis）。"""
    try:
        provider = get_provider()
        row = service.compile_sop_from_media(
            db,
            provider=provider,
            process_key=body.process_key,
            reference_video_asset_id=body.reference_video_asset_id,
            manual_asset_ids=body.manual_asset_ids,
            compiled_by=current_user.id,
            teaching_mode=body.teaching_mode,
        )
    except service.SopCompileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    record_audit(
        db,
        action=AuditAction.provider_call,
        actor_id=current_user.id,
        target_type="sop",
        target_id=row.id,
        detail={
            "process_key": body.process_key,
            "provider": provider.name,
            "reference_video": body.reference_video_asset_id,
        },
    )
    return _out(row)


@router.patch("/{sop_id}", response_model=SopOut, dependencies=[Depends(require_csrf)])
def patch_sop(
    sop_id: str,
    body: SopPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_writer),
) -> SopOut:
    try:
        row = service.update_sop(
            db,
            sop_id,
            steps=body.steps,
            scoring_profile=body.scoring_profile,
            source_grade=body.source_grade,
            course_meta=body.course_meta.model_dump() if body.course_meta else None,
            edited_by=current_user.id,
        )
    except service.SopNotFoundError as exc:
        raise HTTPException(status_code=404, detail="SOP 不存在") from exc
    except service.SopPublishError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _out(row)


@router.post("/{sop_id}/publish", response_model=SopOut, dependencies=[Depends(require_csrf)])
def publish_sop(
    sop_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_writer),
) -> SopOut:
    try:
        row = service.publish_sop(db, sop_id, published_by=current_user.id)
    except service.SopNotFoundError as exc:
        raise HTTPException(status_code=404, detail="SOP 不存在") from exc
    except service.SopPublishError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_audit(
        db,
        action=AuditAction.sop_publish,
        actor_id=current_user.id,
        target_type="sop",
        target_id=row.id,
        detail={"process_key": row.process_key, "version": row.version},
    )
    return _out(row)
