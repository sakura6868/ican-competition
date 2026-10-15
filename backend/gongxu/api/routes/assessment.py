"""考评路由：创建会话、提交判定、报告查询（预录优先）。

learner 只能访问自己的会话/报告；列表按角色过滤；状态变更请求需 CSRF 校验。
整改报告 P0-B：正常预录考评必须绑定真实视频资产，不再接受 scenario；
模型缺失时结构化拒判。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from gongxu.assessment import service
from gongxu.config import get_settings
from gongxu.db.models import (
    AssessmentSession,
    AssessmentStatus,
    AuditAction,
    Job,
    JobStatus,
    Observation,
    ScoreReport,
    SopPublishStatus,
    SopVersion,
    SourceAsset,
    StepAssessment,
    User,
    UserRole,
)
from gongxu.db.session import get_db
from gongxu.jobs import service as jobs_service
from gongxu.jobs.schemas import JobOut, job_out
from gongxu.security import auth
from gongxu.security.audit import record_audit
from gongxu.security.csrf import require_csrf
from gongxu.sop.schema import SopStep

router = APIRouter(tags=["assessment"])

READERS = (UserRole.admin, UserRole.instructor, UserRole.observer, UserRole.learner)
ObservableCheckStatus = Literal["passed", "failed", "insufficient_evidence"]


class SessionCreate(BaseModel):
    process_key: str = Field(min_length=1, max_length=128)
    mode: str = Field(default="recorded", pattern="^(recorded|manual_offline)$")
    sop_version_id: str | None = None
    input_asset_id: str | None = None
    input_sha256: str | None = None


class JudgementIn(BaseModel):
    step_index: int = Field(ge=1)
    judgement: str = Field(pattern="^(passed|failed|unobservable|insufficient_evidence)$")
    start_ms: int = 0
    end_ms: int = 0
    rule_code: str = ""
    reason: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)
    keyframe_ref: str | None = None
    observable_check_results: dict[
        str,
        ObservableCheckStatus,
    ] = Field(default_factory=dict)


class EvaluateRequest(BaseModel):
    judgements: list[JudgementIn] = Field(min_length=1, max_length=32)
    # Kept for request compatibility only. Manual clients cannot assert model provenance.
    model_version: str = service.MANUAL_REVIEW_MODEL_VERSION


class SessionOut(BaseModel):
    id: str
    mode: str
    process_key: str
    status: str
    learner_id: str | None
    input_asset_id: str | None
    input_sha256: str | None
    sop_version_id: str | None
    job_id: str | None
    progress_percent: int
    progress_stage: str
    created_at: object


class ReportOut(BaseModel):
    id: str
    session_id: str
    total_score: float
    result: str
    dimension_scores: dict[str, Any]
    evidence_coverage: float
    model_version: str
    scoring_rules_version: str


def _latest_session_job(db: Session, session_id: str) -> Job | None:
    return jobs_service.latest_for_payload(
        db,
        job_type="recorded_assessment",
        payload_key="session_id",
        payload_value=session_id,
    )


def _session_out(row: AssessmentSession, db: Session) -> SessionOut:
    job = _latest_session_job(db, row.id)
    public_job = job_out(job) if job is not None else None
    return SessionOut(
        id=row.id,
        mode=row.mode,
        process_key=row.process_key,
        status=row.status.value,
        learner_id=row.learner_id,
        input_asset_id=row.input_asset_id,
        input_sha256=row.input_sha256,
        sop_version_id=row.sop_version_id,
        job_id=public_job.id if public_job else None,
        progress_percent=public_job.progress_percent if public_job else 0,
        progress_stage=public_job.stage if public_job else "not_started",
        created_at=row.created_at,
    )


def _report_out(report: ScoreReport) -> ReportOut:
    return ReportOut(
        id=report.id,
        session_id=report.session_id,
        total_score=report.total_score,
        result=report.result,
        dimension_scores=report.dimension_scores,
        evidence_coverage=report.evidence_coverage,
        model_version=report.model_version,
        scoring_rules_version=report.scoring_rules_version,
    )


def _require_writer(current_user: User = Depends(auth.get_current_user)) -> User:
    if current_user.role not in (UserRole.admin, UserRole.instructor):
        raise HTTPException(status_code=403, detail="权限不足")
    return current_user


def _require_owned_session(
    db: Session, user: User, session_id: str, action: str
) -> AssessmentSession:
    """加载会话并执行统一授权；不存在返回 404，无权返回 403。"""
    row = db.get(AssessmentSession, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        service.authorize_assessment_session(user, row, action)
    except service.ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return row


@router.get("/assessment-sessions", response_model=list[SessionOut])
def list_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> list[SessionOut]:
    if current_user.role not in READERS:
        raise HTTPException(status_code=403, detail="权限不足")
    rows = list(db.scalars(select(AssessmentSession).order_by(AssessmentSession.created_at.desc())))
    allowed = service.list_sessions_for(current_user, rows)
    return [_session_out(r, db) for r in allowed]


@router.post(
    "/assessment-sessions",
    response_model=SessionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
def create_session(
    body: SessionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> SessionOut:
    if current_user.role not in READERS:
        raise HTTPException(status_code=403, detail="权限不足")
    if current_user.role == UserRole.observer:
        raise HTTPException(status_code=403, detail="observer 只能读取，不能创建会话")
    # learner 只能创建自己的会话；instructor/admin 可代建（learner_id=None 表示系统发起）
    learner_id = current_user.id if current_user.role == UserRole.learner else None
    input_sha256 = body.input_sha256
    if body.input_asset_id:
        asset = db.get(SourceAsset, body.input_asset_id)
        if asset is None or asset.kind != "video":
            raise HTTPException(status_code=400, detail="考评视频资产不存在或类型不是视频")
        if current_user.role == UserRole.learner and asset.authorized_by != current_user.id:
            raise HTTPException(status_code=403, detail="无权使用其他学员的视频")
        input_sha256 = asset.sha256
    try:
        row = service.create_session(
            db,
            process_key=body.process_key,
            mode=body.mode,
            learner_id=learner_id,
            sop_version_id=body.sop_version_id,
            input_asset_id=body.input_asset_id,
            input_sha256=input_sha256,
        )
    except service.AssessmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _session_out(row, db)


class ProcessResponse(BaseModel):
    job_id: str
    status: str


@router.get("/assessment-sessions/{session_id}", response_model=SessionOut)
def get_session(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> SessionOut:
    row = _require_owned_session(db, current_user, session_id, "read")
    return _session_out(row, db)


class SubmitVideoResponse(BaseModel):
    session: SessionOut
    job_id: str
    job_status: str
    preview_url: str
    cache_hit: bool = False
    cache_source_session_id: str | None = None


@router.post(
    "/assessment-sessions/submit-video",
    response_model=SubmitVideoResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_csrf)],
)
async def submit_video(
    file: UploadFile,
    sop_version_id: str = Form(...),
    process_key: str = Form(""),
    license_note: str = Form("学员考评视频"),
    allow_provider_export: bool = Form(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> SubmitVideoResponse:
    """Upload a learner video, create its session and enqueue analysis in one action."""
    if current_user.role == UserRole.observer:
        raise HTTPException(status_code=403, detail="observer 只能读取，不能提交视频")
    sop = db.get(SopVersion, sop_version_id)
    if sop is None or sop.status != SopPublishStatus.published:
        raise HTTPException(status_code=400, detail="请选择已发布的 SOP")
    resolved_process_key = process_key or sop.process_key
    if resolved_process_key != sop.process_key:
        raise HTTPException(status_code=400, detail="SOP 与工序不匹配")

    from gongxu.assets import service as asset_service

    try:
        asset = await asset_service.register_uploaded_asset(
            db,
            upload=file,
            kind="video",
            license_note=license_note,
            authorized_by=current_user.id,
            allow_provider_export=allow_provider_export,
        )
    except asset_service.AssetTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except asset_service.AssetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    learner_id = current_user.id if current_user.role == UserRole.learner else None
    cached = None
    # Without explicit export consent, do not even construct the external
    # adapter merely to inspect its version.  The queued job will persist the
    # structured provider_export_not_authorized rejection locally.
    if get_settings().vision_backend != "qwen" or asset.allow_provider_export:
        from gongxu.vision.pipeline import _model_version

        current_model_version = _model_version(
            db,
            process_key=sop.process_key,
            sop_steps=[SopStep.model_validate(step) for step in sop.steps],
        )
        cached = service.find_completed_assessment_cache(
            db,
            owner_user_id=current_user.id,
            input_sha256=asset.sha256,
            sop_version_id=sop.id,
            model_version=current_model_version,
        )
    try:
        session = service.create_session(
            db,
            process_key=resolved_process_key,
            mode="recorded",
            learner_id=learner_id,
            sop_version_id=sop.id,
            input_asset_id=asset.id,
            input_sha256=asset.sha256,
        )
    except service.AssessmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit(
        db,
        action=AuditAction.asset_upload,
        actor_id=current_user.id,
        target_type="asset",
        target_id=asset.id,
        detail={"kind": "video", "filename": asset.filename, "workflow": "assessment"},
    )

    if cached is not None:
        report = service.copy_cached_assessment_result(
            db,
            target_session=session,
            cached=cached,
        )
        job = jobs_service.enqueue(
            db,
            job_type="recorded_assessment",
            payload={"session_id": session.id, "requested_by": current_user.id},
            idempotency_key=f"assess:{session.id}",
            max_retries=3,
        )
        job.started_at = session.started_at
        db.commit()
        jobs_service.set_progress(
            db,
            job.id,
            stage="done",
            progress_percent=100,
            status_text="已复用同一视频的既有考评结果",
            details={
                "cache_hit": True,
                "source_session_id": cached.source_session.id,
            },
        )
        cached_coaching = service._coaching_from_job(cached.source_job)
        job = jobs_service.complete(
            db,
            job.id,
            result={
                "session_id": session.id,
                "report_id": report.id,
                "total_score": report.total_score,
                "result": report.result,
                "evidence_coverage": report.evidence_coverage,
                "observations": db.query(Observation)
                .filter(Observation.session_id == session.id)
                .count(),
                "evidence_source": "assessment_cache",
                "model_version": report.model_version,
                "scoring_rules_version": report.scoring_rules_version,
                "cache_hit": True,
                "source_session_id": cached.source_session.id,
                "coaching": cached_coaching,
            },
        )
        db.refresh(session)
        return SubmitVideoResponse(
            session=_session_out(session, db),
            job_id=job.id,
            job_status=job.status.value,
            preview_url=f"/api/v1/assets/{asset.id}/preview",
            cache_hit=True,
            cache_source_session_id=cached.source_session.id,
        )

    job = jobs_service.enqueue(
        db,
        job_type="recorded_assessment",
        payload={"session_id": session.id, "requested_by": current_user.id},
        idempotency_key=f"assess:{session.id}",
        max_retries=3,
    )
    session.status = AssessmentStatus.processing
    db.commit()
    db.refresh(session)
    return SubmitVideoResponse(
        session=_session_out(session, db),
        job_id=job.id,
        job_status=job.status.value,
        preview_url=f"/api/v1/assets/{asset.id}/preview",
        cache_hit=False,
        cache_source_session_id=None,
    )


class StepJudgementOut(BaseModel):
    step_index: int
    step_id: str
    judgement: str
    start_ms: int
    end_ms: int
    rule_code: str
    reason: str
    confidence: float
    keyframe_ref: str | None
    detected_tools: list[str] = Field(default_factory=list)
    detected_objects: list[str] = Field(default_factory=list)
    detected_actions: list[str] = Field(default_factory=list)
    observable_check_results: dict[
        str,
        ObservableCheckStatus,
    ] = Field(default_factory=dict)
    evidence_timestamp_ms: int | None = None
    evidence_url: str | None = None


def _ordered_unique(values: list[str]) -> list[str]:
    """Return non-empty detector labels in first-seen (timeline) order."""
    return list(dict.fromkeys(value for value in values if value))


def _public_observable_check_results(
    values: dict[str, str],
) -> dict[str, ObservableCheckStatus]:
    public: dict[str, ObservableCheckStatus] = {}
    for check, check_status in values.items():
        if check_status == "passed":
            public[check] = "passed"
        elif check_status == "failed":
            public[check] = "failed"
        elif check_status == "insufficient_evidence":
            public[check] = "insufficient_evidence"
    return public


def _keyframe_observation(
    step: StepAssessment,
    observations: list[Observation],
) -> Observation | None:
    """Resolve the stored keyframe hash to an observation in the step interval."""
    if not step.keyframe_ref:
        return None
    start_ms = min(step.start_ms, step.end_ms)
    end_ms = max(step.start_ms, step.end_ms)
    midpoint_ms = start_ms + (end_ms - start_ms) // 2
    matches = [
        observation
        for observation in observations
        if observation.frame_hash == step.keyframe_ref
        and start_ms <= observation.timestamp_ms <= end_ms
    ]
    if not matches:
        return None
    return min(matches, key=lambda observation: abs(observation.timestamp_ms - midpoint_ms))


def _step_judgement_out(
    step: StepAssessment,
    observations: list[Observation],
    *,
    has_video: bool,
) -> StepJudgementOut:
    start_ms = min(step.start_ms, step.end_ms)
    end_ms = max(step.start_ms, step.end_ms)
    interval = [
        observation
        for observation in observations
        if start_ms <= observation.timestamp_ms <= end_ms
    ]
    keyframe_observation = _keyframe_observation(step, interval)
    return StepJudgementOut(
        step_index=step.step_index,
        step_id=step.step_id,
        judgement=step.judgement.value,
        start_ms=step.start_ms,
        end_ms=step.end_ms,
        rule_code=step.rule_code,
        reason=step.reason,
        confidence=step.confidence,
        keyframe_ref=step.keyframe_ref,
        detected_tools=_ordered_unique([item.tool for item in interval]),
        detected_objects=_ordered_unique([item.object for item in interval]),
        detected_actions=_ordered_unique([item.action for item in interval]),
        observable_check_results=_public_observable_check_results(
            step.observable_check_results or {}
        ),
        evidence_timestamp_ms=(
            keyframe_observation.timestamp_ms if keyframe_observation is not None else None
        ),
        evidence_url=(
            f"/api/v1/assessment-sessions/{step.session_id}/evidence-frames/{step.step_index}"
            if has_video and keyframe_observation is not None
            else None
        ),
    )


class PracticePlanOut(BaseModel):
    focus_steps: list[str] = Field(default_factory=list, max_length=5)
    repeat_count: int = Field(ge=1, le=10)
    note: str = Field(max_length=800)


class CoachingOut(BaseModel):
    version: str = Field(max_length=32)
    source: str = Field(
        pattern="^(external_model|external_model_partial|deterministic_rules)$"
    )
    explanation: str = Field(max_length=1800)
    practice_plan: PracticePlanOut
    provider: str | None = Field(default=None, max_length=64)
    model_id: str | None = Field(default=None, max_length=128)
    errors: list[str] = Field(default_factory=list, max_length=4)


class ProviderMetricsOut(BaseModel):
    provider: str = Field(max_length=32)
    model_id: str = Field(max_length=128)
    calls: int = Field(ge=0)
    successful_calls: int = Field(ge=0)
    failed_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    pricing_status: str = Field(max_length=64)


class SessionReportOut(BaseModel):
    report: ReportOut | None
    steps: list[StepJudgementOut]
    rejected: bool = False
    reject_reason: str = ""
    coaching: CoachingOut | None = None
    analysis_duration_ms: int | None = Field(default=None, ge=0)
    provider_metrics: ProviderMetricsOut | None = None


@router.get("/assessment-sessions/{session_id}/report", response_model=SessionReportOut)
def session_report(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> SessionReportOut:
    row = _require_owned_session(db, current_user, session_id, "read")
    report = (
        db.query(ScoreReport)
        .filter(ScoreReport.session_id == session_id)
        .order_by(ScoreReport.created_at.desc())
        .first()
    )
    if report is not None:
        try:
            service.authorize_report(current_user, report, row)
        except service.ForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    steps = (
        db.query(StepAssessment)
        .filter(StepAssessment.session_id == session_id)
        .order_by(StepAssessment.step_index)
        .all()
    )
    observations = (
        db.query(Observation)
        .filter(Observation.session_id == session_id)
        .order_by(Observation.timestamp_ms)
        .all()
    )
    job = _latest_session_job(db, session_id)
    job_result = dict(job.result or {}) if job is not None else {}
    coaching_payload = service._coaching_from_job(job) if job is not None else None
    try:
        coaching = (
            CoachingOut.model_validate(coaching_payload)
            if coaching_payload is not None
            else None
        )
    except ValidationError:
        coaching = None
    analysis_duration_ms = None
    if job is not None and job.started_at is not None and job.finished_at is not None:
        analysis_duration_ms = max(
            0,
            round((job.finished_at - job.started_at).total_seconds() * 1000),
        )
    provider_metrics = None
    raw_provider_metrics = job_result.get("provider_metrics")
    if isinstance(raw_provider_metrics, dict) and raw_provider_metrics:
        try:
            provider_metrics = ProviderMetricsOut.model_validate(raw_provider_metrics)
        except ValidationError:
            provider_metrics = None
    return SessionReportOut(
        report=_report_out(report) if report is not None else None,
        steps=[
            _step_judgement_out(step, observations, has_video=bool(row.input_asset_id))
            for step in steps
        ],
        rejected=bool(job_result.get("rejected", False)),
        reject_reason=str(job_result.get("reason") or ""),
        coaching=coaching,
        analysis_duration_ms=analysis_duration_ms,
        provider_metrics=provider_metrics,
    )


class SessionProgressOut(BaseModel):
    session_id: str
    session_status: str
    job: JobOut | None
    report_ready: bool
    report_id: str | None
    analyzed_frames: int | None
    estimated_remaining_seconds: int | None


@router.get("/assessment-sessions/{session_id}/progress", response_model=SessionProgressOut)
def session_progress(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> SessionProgressOut:
    row = _require_owned_session(db, current_user, session_id, "read")
    job = _latest_session_job(db, session_id)
    public_job = job_out(job) if job is not None else None
    report = (
        db.query(ScoreReport)
        .filter(ScoreReport.session_id == session_id)
        .order_by(ScoreReport.created_at.desc())
        .first()
    )
    checkpoint = public_job.checkpoint if public_job else {}
    raw_frames = checkpoint.get("analyzed_frames")
    raw_eta = checkpoint.get("estimated_remaining_seconds")
    report_ready = bool(
        report is not None
        and row.status == AssessmentStatus.completed
        and (
            row.mode == "manual_offline"
            or (job is not None and job.status == JobStatus.succeeded)
        )
    )
    return SessionProgressOut(
        session_id=row.id,
        session_status=row.status.value,
        job=public_job,
        report_ready=report_ready,
        report_id=report.id if report is not None else None,
        analyzed_frames=int(raw_frames) if isinstance(raw_frames, (int, float)) else None,
        estimated_remaining_seconds=int(raw_eta) if isinstance(raw_eta, (int, float)) else None,
    )


class ObservationOut(BaseModel):
    timestamp_ms: int
    hand: str
    tool: str
    object: str
    action: str
    relation: str
    confidence: float


class SessionEvidenceOut(BaseModel):
    session_id: str
    input_asset_id: str | None
    preview_url: str | None
    report_id: str | None
    steps: list[StepJudgementOut]
    observations: list[ObservationOut]


@router.get("/assessment-sessions/{session_id}/evidence", response_model=SessionEvidenceOut)
def session_evidence(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> SessionEvidenceOut:
    row = _require_owned_session(db, current_user, session_id, "read")
    report = (
        db.query(ScoreReport)
        .filter(ScoreReport.session_id == session_id)
        .order_by(ScoreReport.created_at.desc())
        .first()
    )
    steps = (
        db.query(StepAssessment)
        .filter(StepAssessment.session_id == session_id)
        .order_by(StepAssessment.step_index)
        .all()
    )
    observations = (
        db.query(Observation)
        .filter(Observation.session_id == session_id)
        .order_by(Observation.timestamp_ms)
        .all()
    )
    return SessionEvidenceOut(
        session_id=row.id,
        input_asset_id=row.input_asset_id,
        preview_url=(
            f"/api/v1/assets/{row.input_asset_id}/preview" if row.input_asset_id else None
        ),
        report_id=report.id if report else None,
        steps=[
            _step_judgement_out(step, observations, has_video=bool(row.input_asset_id))
            for step in steps
        ],
        observations=[
            ObservationOut(
                timestamp_ms=observation.timestamp_ms,
                hand=observation.hand,
                tool=observation.tool,
                object=observation.object,
                action=observation.action,
                relation=observation.relation,
                confidence=observation.confidence,
            )
            for observation in observations
        ],
    )


@router.get("/assessment-sessions/{session_id}/evidence-frames/{step_index}")
def session_evidence_frame(
    session_id: str,
    step_index: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> FileResponse:
    """Extract one authenticated evidence JPEG on demand without persisting plaintext."""
    row = _require_owned_session(db, current_user, session_id, "read")
    if not row.input_asset_id:
        raise HTTPException(status_code=404, detail="该会话没有可提取证据帧的视频")
    step = (
        db.query(StepAssessment)
        .filter(
            StepAssessment.session_id == session_id,
            StepAssessment.step_index == step_index,
        )
        .first()
    )
    if step is None:
        raise HTTPException(status_code=404, detail="步骤证据不存在")
    if not step.keyframe_ref:
        raise HTTPException(status_code=404, detail="该步骤没有可追溯的主要证据帧")

    observations = (
        db.query(Observation)
        .filter(
            Observation.session_id == session_id,
            Observation.frame_hash == step.keyframe_ref,
        )
        .order_by(Observation.timestamp_ms)
        .all()
    )
    keyframe_observation = _keyframe_observation(step, observations)
    if keyframe_observation is None:
        raise HTTPException(status_code=404, detail="主要证据帧与观测记录不匹配")

    from gongxu.assets import service as asset_service
    from gongxu.media.video import MediaError, extract_frame_at

    settings = get_settings()
    preview_root = settings.data_root / "runtime" / "evidence-previews"
    preview_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f"evidence_{session_id[:8]}_", dir=preview_root))
    try:
        video_path = asset_service.decrypt_to_temp(db, row.input_asset_id, temp_dir=temp_dir)
        frame_path = extract_frame_at(
            video_path,
            temp_dir / f"step_{step_index:02d}.jpg",
            timestamp_ms=keyframe_observation.timestamp_ms,
        )
    except asset_service.AssetNotFoundError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=404, detail="考评视频文件不存在") from exc
    except MediaError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return FileResponse(
        frame_path,
        media_type="image/jpeg",
        filename=f"step-{step_index:02d}-evidence.jpg",
        content_disposition_type="inline",
        background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
        headers={"Cache-Control": "private, no-store"},
    )


@router.post(
    "/assessment-sessions/{session_id}/process",
    response_model=ProcessResponse,
    dependencies=[Depends(require_csrf)],
)
def process_recorded(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> ProcessResponse:
    """入队预录视频自动评分任务（整改报告 P0-B：绑定真实视频，无 scenario）。"""
    session = _require_owned_session(db, current_user, session_id, "run")
    if session.mode != "recorded":
        raise HTTPException(status_code=400, detail="只有预录模式支持视频处理")
    if not session.sop_version_id:
        raise HTTPException(
            status_code=400,
            detail="历史考评会话未绑定明确的 SOP 版本，无法处理；请重新创建会话",
        )
    try:
        service._load_sop(db, session.sop_version_id, session.process_key)
    except service.AssessmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not session.input_asset_id:
        raise HTTPException(status_code=400, detail="考评会话必须绑定预录视频资产")
    asset = db.get(SourceAsset, session.input_asset_id)
    if asset is None:
        raise HTTPException(status_code=400, detail="预录视频资产不存在")
    if asset.kind != "video":
        raise HTTPException(status_code=400, detail="会话绑定资产必须是视频")
    if current_user.role == UserRole.learner and asset.authorized_by != current_user.id:
        raise HTTPException(status_code=403, detail="无权处理其他学员的视频")
    # 模型缺失时在任务执行阶段结构化拒判，不在这里硬拒绝（任务可复现拒判原因）
    job = jobs_service.enqueue(
        db,
        job_type="recorded_assessment",
        payload={"session_id": session_id, "requested_by": current_user.id},
        idempotency_key=f"assess:{session_id}",
        max_retries=3,
    )
    if job.status.value in ("queued", "running", "retrying"):
        session.status = AssessmentStatus.processing
        db.commit()
    return ProcessResponse(job_id=job.id, status=job.status.value)


@router.post(
    "/assessment-sessions/{session_id}/evaluate",
    response_model=ReportOut,
    dependencies=[Depends(require_csrf)],
)
def evaluate(
    session_id: str,
    body: EvaluateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> ReportOut:
    _require_owned_session(db, current_user, session_id, "score")
    try:
        report = service.evaluate(
            db,
            session_id,
            judgements=[j.model_dump() for j in body.judgements],
            model_version=service.MANUAL_REVIEW_MODEL_VERSION,
        )
    except service.AssessmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _report_out(report)


@router.get("/reports/{report_id}", response_model=ReportOut)
def get_report(
    report_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> ReportOut:
    try:
        report = service.get_report(db, report_id)
    except service.AssessmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="报告不存在") from exc
    session = db.get(AssessmentSession, report.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    try:
        service.authorize_report(current_user, report, session)
    except service.ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return _report_out(report)
