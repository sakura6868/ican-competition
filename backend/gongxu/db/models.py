"""SQLAlchemy 模型：阶段 2 实现账号底座。

表：users / auth_sessions / audit_logs / jobs（SQLite 持久化任务队列）。
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gongxu.db.session import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    """naive UTC now：SQLite 不保存 tzinfo，统一使用 naive UTC 避免 aware/naive 比较错误。"""
    return datetime.now(UTC).replace(tzinfo=None)


class UserRole(enum.StrEnum):
    admin = "admin"
    instructor = "instructor"
    learner = "learner"
    observer = "observer"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, native_enum=False), default=UserRole.learner
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    ip_address: Mapped[str] = mapped_column(String(64), default="")

    user: Mapped[User] = relationship(back_populates="sessions")


class AuditAction(enum.StrEnum):
    login = "login"
    logout = "logout"
    password_change = "password_change"  # nosec B105：审计动作名，非密码
    user_create = "user_create"
    user_update = "user_update"
    asset_upload = "asset_upload"
    provider_call = "provider_call"
    sop_publish = "sop_publish"
    assessment = "assessment"
    report_access = "report_access"
    export = "export"
    cleanup = "cleanup"
    decrypt_failure = "decrypt_failure"
    other = "other"


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    action: Mapped[AuditAction] = mapped_column(Enum(AuditAction, native_enum=False), index=True)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    retrying = "retrying"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False), default=JobStatus.queued, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # 阶段检查点
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PracticeSession(Base):
    __tablename__ = "practice_sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    sop_id: Mapped[str] = mapped_column(ForeignKey("sop_versions.id"), index=True)
    learner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    state: Mapped[dict[str, Any]] = mapped_column(JSON)
    allow_provider_export: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PracticeObservation(Base):
    __tablename__ = "practice_observations"
    __table_args__ = (UniqueConstraint("session_id", "request_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("practice_sessions.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(80))
    step_revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    frames: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    encrypted_frames: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    receipt_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PracticeEvent(Base):
    __tablename__ = "practice_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("practice_sessions.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceGrade(enum.StrEnum):
    """来源等级（与 gongxu.sop.schema 保持一致）。"""

    video_reference = "video_reference"
    manual_backed = "manual_backed"
    sensor_verified = "sensor_verified"


class SopPublishStatus(enum.StrEnum):
    draft = "draft"
    published = "published"
    archived = "archived"


class SourceAsset(Base):
    """标准教学资产（视频/手册/图片）登记。原始文件加密存储，见阶段 4。"""

    __tablename__ = "source_assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(32))  # video | manual | image
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    license_note: Mapped[str] = mapped_column(String(255), default="")
    authorized_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    encrypted_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 出站隐私（整改 P1-1）：是否允许把该资产关键帧/文本发送给外部多模态 Provider。
    # 逐资产授权，默认 False。教师上传时必须显式勾选。
    allow_provider_export: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProviderReceipt(Base):
    """一次 Provider 调用的持久化收据（可复现性与审计）。"""

    __tablename__ = "provider_receipts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    model_id: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(64))
    input_digest: Mapped[str] = mapped_column(String(32))
    response_digest: Mapped[str] = mapped_column(String(32))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SopVersion(Base):
    """SOP 版本。steps 以 JSON 快照保存：版本不可变，天然支持证据审计与复现。"""

    __tablename__ = "sop_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    process_key: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[SopPublishStatus] = mapped_column(
        Enum(SopPublishStatus, native_enum=False), default=SopPublishStatus.draft, index=True
    )
    source_grade: Mapped[SourceGrade] = mapped_column(
        Enum(SourceGrade, native_enum=False), default=SourceGrade.video_reference
    )
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # SopStep JSON 快照列表
    course_meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    scoring_profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prompt_version: Mapped[str] = mapped_column(String(64), default="sop-compile-v1")
    provider_receipt_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 来源资产追踪（整改 P0-2/P1-1）：标准参考视频与手册资产；供来源失效检测。
    reference_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    compiled_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProcessVocabulary(Base):
    """教师确认的工序受控词表；未确认提案不得用于发布门禁。"""

    __tablename__ = "process_vocabularies"

    process_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    objects: Mapped[list[str]] = mapped_column(JSON, default=list)
    actions: Mapped[list[str]] = mapped_column(JSON, default=list)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source: Mapped[str] = mapped_column(String(32), default="compiled_proposal")
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DocumentChunk(Base):
    """手册/规范文本切片，AES-256-GCM 加密保存（handoff §8）。

    授权检索时在内存解密；原始明文不入库。
    """

    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("source_assets.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    content_encrypted: Mapped[str] = mapped_column(Text)  # gx1:<b64>
    content_hash: Mapped[str] = mapped_column(String(64), index=True)  # sha256
    source_asset: Mapped[SourceAsset] = relationship()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DerivedAsset(Base):
    """资产衍生物（代理视频/关键帧/证据片段），AES-256-GCM 加密存储（整改 R3）。

    只用于单次模型推理的关键帧不进入长期存储；长期衍生物在权限受限的
    任务临时目录短暂解密，处理后立即清理。
    """

    __tablename__ = "derived_assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    source_asset_id: Mapped[str] = mapped_column(
        ForeignKey("source_assets.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))  # proxy_video | keyframe | evidence_clip
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    encrypted_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    frame_index: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 关键帧序号
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Workstation(Base):
    """工位登记：摄像头配置、ROI 与标定状态（handoff §6/§7）。"""

    __tablename__ = "workstations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128))
    camera_id: Mapped[str] = mapped_column(String(64), default="")
    resolution: Mapped[str] = mapped_column(String(32), default="1920x1080")
    roi: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {x,y,w,h}
    calibration_state: Mapped[str] = mapped_column(String(32), default="uncalibrated")
    calibration_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # homography 等
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class WorkstationMarker(Base):
    """AprilTag/已知点位：标定坐标与像素坐标。"""

    __tablename__ = "workstation_markers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workstation_id: Mapped[str] = mapped_column(
        ForeignKey("workstations.id", ondelete="CASCADE"), index=True
    )
    marker_id: Mapped[str] = mapped_column(String(64))
    pixel_x: Mapped[float] = mapped_column(Float, default=0.0)
    pixel_y: Mapped[float] = mapped_column(Float, default=0.0)
    world_x: Mapped[float] = mapped_column(Float, default=0.0)
    world_y: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CatalogItem(Base):
    """工具/部件目录：名称、类别、示例图像与使用工序。"""

    __tablename__ = "catalog_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(32))  # tool | part
    name: Mapped[str] = mapped_column(String(128), index=True)
    category: Mapped[str] = mapped_column(String(64), default="")
    sample_image_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    used_in_process: Mapped[str] = mapped_column(String(128), default="")
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssessmentStatus(enum.StrEnum):
    created = "created"
    calibrating = "calibrating"
    ready = "ready"
    running = "running"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class StepJudgement(enum.StrEnum):
    passed = "passed"
    failed = "failed"
    unobservable = "unobservable"
    insufficient_evidence = "insufficient_evidence"


class AssessmentSession(Base):
    """一次预录/实时考评会话（handoff §7/§9/§10）。"""

    __tablename__ = "assessment_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # recorded = worker-backed automatic video assessment; manual_offline = instructor review.
    mode: Mapped[str] = mapped_column(String(16), default="recorded")
    process_key: Mapped[str] = mapped_column(String(128), index=True)
    sop_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workstation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[AssessmentStatus] = mapped_column(
        Enum(AssessmentStatus, native_enum=False), default=AssessmentStatus.created, index=True
    )
    learner_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    input_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 预录视频
    input_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Observation(Base):
    """观察事件流：手部/工具/部件/动作/状态（handoff §6 事件格式）。"""

    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_sessions.id", ondelete="CASCADE"), index=True
    )
    timestamp_ms: Mapped[int] = mapped_column(Integer)
    hand: Mapped[str] = mapped_column(String(16), default="")  # left | right
    tool: Mapped[str] = mapped_column(String(64), default="")
    object: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64), default="")
    relation: Mapped[str] = mapped_column(String(64), default="")  # holds/contacts/...
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    frame_hash: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class StepAssessment(Base):
    """步骤级判定与证据绑定（每步 passed/failed/unobservable/insufficient_evidence）。"""

    __tablename__ = "step_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_sessions.id", ondelete="CASCADE"), index=True
    )
    step_index: Mapped[int] = mapped_column(Integer)
    step_id: Mapped[str] = mapped_column(String(64))
    judgement: Mapped[StepJudgement] = mapped_column(
        Enum(StepJudgement, native_enum=False), default=StepJudgement.insufficient_evidence
    )
    start_ms: Mapped[int] = mapped_column(Integer, default=0)
    end_ms: Mapped[int] = mapped_column(Integer, default=0)
    rule_code: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(String(512), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    keyframe_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    observable_check_results: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScoreReport(Base):
    """确定性评分报告（分数由规则引擎计算，大模型不得修改）。"""

    __tablename__ = "score_reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("assessment_sessions.id", ondelete="CASCADE"), index=True
    )
    total_score: Mapped[float] = mapped_column(Float, default=0.0)
    result: Mapped[str] = mapped_column(
        String(32), default="inconclusive"
    )  # pass/fail/inconclusive
    dimension_scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence_coverage: Mapped[float] = mapped_column(Float, default=0.0)
    model_version: Mapped[str] = mapped_column(String(64), default="")
    input_hash: Mapped[str] = mapped_column(String(64), default="")
    scoring_rules_version: Mapped[str] = mapped_column(String(64), default="scoring-v5")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelStatus(enum.StrEnum):
    available = "available"
    experimental = "experimental"
    missing = "missing"


class ModelRegistryEntry(Base):
    """本地模型登记（handoff §7/§8）：模型缺失时可降级并明确拒判。"""

    __tablename__ = "model_registry"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), index=True)  # hand | tool_detector | tcn | pose
    kind: Mapped[str] = mapped_column(String(32))  # hand | detector | sequence | pose
    status: Mapped[ModelStatus] = mapped_column(
        Enum(ModelStatus, native_enum=False), default=ModelStatus.missing
    )
    format: Mapped[str] = mapped_column(String(16), default="onnx")
    version: Mapped[str] = mapped_column(String(32), default="")
    sha256: Mapped[str] = mapped_column(String(64), default="")
    path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
