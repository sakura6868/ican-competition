"""SOP 数据契约（handoff §6）。

来源等级：video_reference / manual_backed / sensor_verified。
步骤判定：passed / failed / unobservable / insufficient_evidence。
"""

from __future__ import annotations

import enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SourceGrade(enum.StrEnum):
    video_reference = "video_reference"
    manual_backed = "manual_backed"
    sensor_verified = "sensor_verified"


class SopStatus(enum.StrEnum):
    draft = "draft"
    published = "published"
    archived = "archived"


class StepStatus(enum.StrEnum):
    passed = "passed"
    failed = "failed"
    unobservable = "unobservable"
    insufficient_evidence = "insufficient_evidence"


class EvidenceRef(BaseModel):
    """字段来源证据：视频时间点或手册页码。"""

    kind: Literal["video", "manual", "sensor"]
    ref: str  # "00:12-00:18" 或 "手册p.12" 或 "sensor#cal-01"
    description: str = ""
    asset_id: str | None = None
    timestamp_ms: int | None = Field(default=None, ge=0)
    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    chunk_id: int | None = Field(default=None, ge=1)
    chunk_index: int | None = Field(default=None, ge=0)
    content_hash: str | None = None


class StepTeaching(BaseModel):
    source_asset_id: str = Field(min_length=1, max_length=32)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    instruction: str = Field(default="", max_length=4000)
    reference_timestamps_ms: list[int] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_range(self) -> StepTeaching:
        if self.end_ms <= self.start_ms:
            raise ValueError("教学片段结束时间必须晚于开始时间")
        if any(not self.start_ms <= t <= self.end_ms for t in self.reference_timestamps_ms):
            raise ValueError("参考图时间必须在本步骤教学片段内")
        return self


class VerificationCheck(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    criterion: str = Field(min_length=1, max_length=1000)
    category: Literal["completion", "safety"] = "completion"
    required: bool = True
    mode: Literal["visual", "manual"] = "visual"
    correction: str = Field(default="", max_length=1000)


class StepVerification(BaseModel):
    checks: list[VerificationCheck] = Field(default_factory=list, max_length=24)
    confirm_windows: int = Field(default=1, ge=1, le=4)
    teacher_confirmed: bool = False

    @model_validator(mode="after")
    def unique_checks(self) -> StepVerification:
        ids = [check.id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("核验条件 ID 不可重复")
        return self


class SopStep(BaseModel):
    """结构化步骤。

    对包含 ``completion_checks`` 或 ``observable_checks`` 的步骤，非空的
    tools/objects/actions 各自构成一个
    必须由视觉证据覆盖的语义类别，类别内多个值表示可接受备选项。无法从画面验证的
    条件应只写入 ``unobservable_checks``，不要伪装成结构化视觉语义。
    ``completion_checks`` 描述动作结束后应该能看见的工件状态，例如“滤芯已就位”；
    ``observable_checks`` 仅描述独立的可观察安全规范，例如“绝缘手套已佩戴”。
    两者都需要逐项视觉证据，但只有后者进入安全维度，避免把“做完了”误记成安全得分。
    ``optional`` 表示该步骤不进入必做评分及覆盖率分母。
    """

    id: str
    index: int = Field(ge=1)
    name: str
    predecessors: list[int] = Field(default_factory=list)  # 依赖前置步骤的 index
    optional: bool = False
    critical: bool = False
    tools: list[str] = Field(default_factory=list)
    objects: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    expected_duration_s: int = Field(default=60, ge=1)
    completion_checks: list[str] = Field(default_factory=list)
    observable_checks: list[str] = Field(default_factory=list)
    unobservable_checks: list[str] = Field(default_factory=list)
    weight: float = Field(gt=0)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    teaching: StepTeaching | None = None
    verification: StepVerification | None = None

    @model_validator(mode="after")
    def sync_checks(self) -> SopStep:
        # Structured conditions are canonical; legacy scoring receives the same criteria.
        if self.verification is not None:
            self.completion_checks = [
                c.criterion
                for c in self.verification.checks
                if c.mode == "visual" and c.category == "completion"
            ]
            self.observable_checks = [
                c.criterion
                for c in self.verification.checks
                if c.mode == "visual" and c.category == "safety"
            ]
            self.unobservable_checks = [
                c.criterion for c in self.verification.checks if c.mode == "manual"
            ]
            # Ordinary teaching steps advance after one temporal burst. Critical
            # steps and steps with required safety checks keep two independent
            # cloud confirmations, regardless of older stored draft values.
            has_required_safety_check = any(
                c.required and c.category == "safety" for c in self.verification.checks
            )
            self.verification.confirm_windows = (
                2 if self.critical or has_required_safety_check else 1
            )
        return self


class ScoringProfile(BaseModel):
    completeness: float = 0.30
    order: float = 0.20
    tools_parts: float = 0.15
    action_quality: float = 0.20
    efficiency: float = 0.05
    observable_safety: float = 0.10


class CourseMetadata(BaseModel):
    title: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=1000)
    objectives: list[str] = Field(default_factory=list, max_length=8)
    difficulty: Literal["入门", "进阶", "挑战"] = "入门"
    audience: str = Field(default="零基础学习者", max_length=120)
    estimated_minutes: int = Field(default=10, ge=1, le=600)


class SopVersion(BaseModel):
    id: str
    process_key: str
    version: int = 1
    status: SopStatus = SopStatus.draft
    source_grade: SourceGrade = SourceGrade.video_reference
    steps: list[SopStep] = Field(default_factory=list)
    course_meta: CourseMetadata = Field(default_factory=CourseMetadata)
    scoring_profile: ScoringProfile = Field(default_factory=ScoringProfile)
    prompt_version: str = "sop-compile-v1"
    provider_receipt_id: str = ""


class ScoringThresholds(BaseModel):
    relation_confidence: float = 0.60
    min_visible_frame_ratio: float = 0.60
    min_evidence_seconds: float = 1.0
