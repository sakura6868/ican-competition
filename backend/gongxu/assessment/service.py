"""考评会话服务：创建、提交判定、生成确定性报告（handoff §9/§10）。

评分由规则引擎计算；步骤判定（对齐）由视觉/对齐模块提供，本模块与之解耦。
首版判定来自调用方（预录/实时），后续接入阶段 7 视觉事实层。
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from gongxu.db.models import (
    AssessmentSession,
    AssessmentStatus,
    Job,
    JobStatus,
    Observation,
    ScoreReport,
    SopPublishStatus,
    SopVersion,
    SourceAsset,
    StepAssessment,
    StepJudgement,
    User,
    UserRole,
    utcnow,
)
from gongxu.scoring.engine import StepResult, score_assessment
from gongxu.sop.schema import (
    ScoringProfile,
    SopStep,
    SourceGrade,
)
from gongxu.sop.schema import (
    SopVersion as SopSchema,
)


class AssessmentError(Exception):
    pass


class AssessmentNotFoundError(AssessmentError):
    pass


class ForbiddenError(AssessmentError):
    """无资源权限。"""


class UnauthorizedError(AssessmentError):
    """未登录或会话失效。"""


ProgressCallback = Callable[[str, int, str, dict[str, Any] | None], None]
SCORING_RULES_VERSION = "scoring-v6"
MANUAL_REVIEW_MODEL_VERSION = "manual_review"
RECORDED_REPORT_PROVENANCE = "recorded_assessment_worker_v1"
_OBSERVABLE_CHECK_STATUSES = {"passed", "failed", "insufficient_evidence"}
_UNCACHEABLE_MODEL_MARKERS = (
    "manual",
    "fixture",
    "replay",
    "no-vision",
    "no_vision",
    "not-invoked",
    "not_invoked",
    "unavailable",
)


@dataclass(frozen=True)
class CachedAssessment:
    """A completed model result that may be copied without re-running inference."""

    source_session: AssessmentSession
    report: ScoreReport
    source_job: Job


def _emit_progress(
    callback: ProgressCallback | None,
    stage: str,
    progress_percent: int,
    status_text: str,
    details: dict[str, Any] | None = None,
) -> None:
    if callback is not None:
        callback(stage, progress_percent, status_text, details)


def authorize_assessment_session(user: User, session: AssessmentSession, action: str) -> None:
    """统一考评会话授权。

    角色规则：
    - admin：全局。
    - instructor：可运行/评分任何会话（首版无班级关系，明确放宽并记录）。
    - learner：只能访问自己的会话；读取/运行/报告均受归属限制。
    - observer：只读。
    """
    if user.role == UserRole.admin:
        return
    if user.role == UserRole.observer:
        if action in ("run", "score", "finish"):
            raise ForbiddenError("observer 只能读取")
        return
    if user.role == UserRole.instructor:
        # 首版无班级关系：instructor 可查看/运行/评分全部会话；报告中注明此放宽。
        return
    # learner：只能访问自己的会话；可创建/查看/运行（评分权归 instructor/admin）
    if not session.learner_id or session.learner_id != user.id:
        raise ForbiddenError("无权访问其他学员的会话")
    if action == "score":
        raise ForbiddenError("评分由教师/管理员执行")
    return


def list_sessions_for(user: User, sessions: list[AssessmentSession]) -> list[AssessmentSession]:
    """按角色过滤会话列表；learner 只返回自己的会话（P0-A.8）。"""
    if user.role == UserRole.learner:
        return [s for s in sessions if s.learner_id == user.id]
    return sessions


def authorize_report(user: User, report: ScoreReport, session: AssessmentSession) -> None:
    """报告访问归属校验：learner 只能看自己的报告。"""
    if user.role == UserRole.learner and session.learner_id != user.id:
        raise ForbiddenError("无权查看其他学员的报告")


def _report_has_cacheable_model(model_version: str) -> bool:
    normalized = model_version.strip().lower()
    return bool(normalized) and not any(
        marker in normalized for marker in _UNCACHEABLE_MODEL_MARKERS
    )


def _same_numeric_result(value: object, expected: float) -> bool:
    try:
        return float(value) == expected  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _bind_report_to_recorded_job(
    *,
    job: Job,
    session: AssessmentSession,
    report: ScoreReport,
    evidence_source: str,
) -> None:
    """Bind an automatic report to its exact worker job in the same transaction.

    The checkpoint is intentionally independent from ``Job.result``: a worker
    can crash after the report commit but before ``jobs.complete``.  Recovery
    accepts only this transactionally persisted binding, never an arbitrary
    completed report that happened to exist on the session.
    """
    payload = job.payload or {}
    if job.job_type != "recorded_assessment" or str(payload.get("session_id", "")) != session.id:
        raise AssessmentError("自动报告与 recorded_assessment 任务不匹配")
    checkpoint = dict(job.checkpoint or {})
    checkpoint.update(
        {
            "report_provenance": RECORDED_REPORT_PROVENANCE,
            "report_job_id": job.id,
            "report_session_id": session.id,
            "report_id": report.id,
            "report_model_version": report.model_version,
            "report_scoring_rules_version": report.scoring_rules_version,
            "report_evidence_source": evidence_source,
        }
    )
    job.checkpoint = checkpoint


def _recoverable_recorded_report(
    db: Session,
    *,
    job: Job,
    session: AssessmentSession,
) -> tuple[ScoreReport, str] | None:
    """Return only a report transactionally bound to this exact worker job."""
    checkpoint = job.checkpoint or {}
    if checkpoint.get("report_provenance") != RECORDED_REPORT_PROVENANCE:
        return None
    if str(checkpoint.get("report_job_id", "")) != job.id:
        return None
    if str(checkpoint.get("report_session_id", "")) != session.id:
        return None
    report_id = str(checkpoint.get("report_id", ""))
    report = db.get(ScoreReport, report_id) if report_id else None
    if report is None or report.session_id != session.id:
        return None
    if session.status != AssessmentStatus.completed:
        return None
    if report.model_version != str(checkpoint.get("report_model_version", "")):
        return None
    if report.scoring_rules_version != str(
        checkpoint.get("report_scoring_rules_version", "")
    ):
        return None
    evidence_source = str(checkpoint.get("report_evidence_source", ""))
    if evidence_source not in {"vision", "fixture", "vision_rejection"}:
        return None
    return report, evidence_source


def _coaching_from_job(job: Job) -> dict[str, Any] | None:
    """Return the persisted coaching payload from a completed or recovering job."""
    for container in (job.result or {}, job.checkpoint or {}):
        value = container.get("coaching")
        if isinstance(value, dict):
            return copy.deepcopy(value)
    return None


def _coaching_steps(db: Session, session_id: str) -> list[dict[str, Any]]:
    """Build a privacy-minimized view of deterministic step judgements."""
    rows = (
        db.query(StepAssessment)
        .filter(StepAssessment.session_id == session_id)
        .order_by(StepAssessment.step_index)
        .all()
    )
    return [
        {
            "step_index": row.step_index,
            "step_id": row.step_id,
            "judgement": row.judgement.value,
            "rule_code": row.rule_code,
            "reason": row.reason,
            "confidence": row.confidence,
            "observable_check_results": copy.deepcopy(row.observable_check_results or {}),
        }
        for row in rows
    ]


def _generate_recorded_coaching(
    db: Session,
    *,
    job: Job,
    session: AssessmentSession,
    report: ScoreReport,
    evidence_source: str,
) -> dict[str, Any]:
    """Generate non-scoring feedback and persist it in the recoverable checkpoint.

    External generation is allowed only for a genuine vision result whose source
    asset has explicit export consent.  The provider receives structured step
    judgements only; no frame, path, hash, video, learner identity, or numeric
    total score is included.
    """
    from gongxu.assessment.coaching import generate_coaching

    source_asset = (
        db.get(SourceAsset, session.input_asset_id) if session.input_asset_id else None
    )
    external_allowed = bool(
        evidence_source == "vision"
        and source_asset is not None
        and source_asset.allow_provider_export
    )
    step_results = _coaching_steps(db, session.id)
    report_summary = {
        "result": report.result,
        "evidence_coverage": report.evidence_coverage,
        "dimension_scores": copy.deepcopy(report.dimension_scores or {}),
    }
    try:
        coaching = generate_coaching(
            db,
            step_results=step_results,
            report=report_summary,
            external_allowed=external_allowed,
        )
    except Exception:  # noqa: BLE001 - optional feedback must never invalidate a score
        db.rollback()
        coaching = generate_coaching(
            db,
            step_results=step_results,
            report=report_summary,
            external_allowed=False,
        )
        coaching["errors"] = ["coaching_generation_failed"]

    checkpoint = dict(job.checkpoint or {})
    checkpoint["coaching"] = copy.deepcopy(coaching)
    job.checkpoint = checkpoint
    db.commit()
    return coaching


def find_completed_assessment_cache(
    db: Session,
    *,
    owner_user_id: str,
    input_sha256: str,
    sop_version_id: str,
    model_version: str,
    scoring_rules_version: str = SCORING_RULES_VERSION,
) -> CachedAssessment | None:
    """Find an owner-scoped, non-fixture result produced by the normal worker.

    SHA256 alone is deliberately insufficient: the immutable SOP snapshot, the
    active vision model, the deterministic scoring rules, and the uploader must
    all match.  Requiring a succeeded recorded-assessment job prevents manually
    submitted judgements from being mistaken for model inference output.
    """
    candidates = (
        db.query(ScoreReport, AssessmentSession)
        .join(AssessmentSession, AssessmentSession.id == ScoreReport.session_id)
        .filter(
            AssessmentSession.mode == "recorded",
            AssessmentSession.status == AssessmentStatus.completed,
            AssessmentSession.input_sha256 == input_sha256,
            AssessmentSession.sop_version_id == sop_version_id,
            ScoreReport.input_hash == input_sha256,
            ScoreReport.model_version == model_version,
            ScoreReport.scoring_rules_version == scoring_rules_version,
        )
        .order_by(ScoreReport.created_at.desc())
        .all()
    )
    succeeded_jobs = (
        db.query(Job)
        .filter(Job.job_type == "recorded_assessment", Job.status == JobStatus.succeeded)
        .order_by(Job.created_at.desc())
        .all()
    )
    for report, source_session in candidates:
        if not _report_has_cacheable_model(report.model_version):
            continue
        if not source_session.input_asset_id:
            continue
        source_asset = db.get(SourceAsset, source_session.input_asset_id)
        if (
            source_asset is None
            or source_asset.authorized_by != owner_user_id
            or source_asset.sha256 != input_sha256
        ):
            continue
        source_job = next(
            (
                job
                for job in succeeded_jobs
                if str((job.payload or {}).get("session_id", "")) == source_session.id
                and str((job.payload or {}).get("requested_by", "")) == owner_user_id
            ),
            None,
        )
        if source_job is None:
            continue
        job_result = source_job.result or {}
        if job_result.get("evidence_source") != "vision":
            continue
        if str(job_result.get("session_id", "")) != source_session.id:
            continue
        if str(job_result.get("report_id", "")) != report.id:
            continue
        if str(job_result.get("model_version", "")) != report.model_version:
            continue
        if str(job_result.get("scoring_rules_version", "")) != report.scoring_rules_version:
            continue
        if str(job_result.get("result", "")) != report.result:
            continue
        if not _same_numeric_result(job_result.get("total_score"), report.total_score):
            continue
        if not _same_numeric_result(
            job_result.get("evidence_coverage"), report.evidence_coverage
        ):
            continue
        recovered = _recoverable_recorded_report(db, job=source_job, session=source_session)
        if recovered is None or recovered[0].id != report.id or recovered[1] != "vision":
            continue
        if db.query(Observation).filter(Observation.session_id == source_session.id).count() == 0:
            continue
        if (
            db.query(StepAssessment)
            .filter(StepAssessment.session_id == source_session.id)
            .count()
            == 0
        ):
            continue
        return CachedAssessment(
            source_session=source_session,
            report=report,
            source_job=source_job,
        )
    return None


def copy_cached_assessment_result(
    db: Session,
    *,
    target_session: AssessmentSession,
    cached: CachedAssessment,
) -> ScoreReport:
    """Copy immutable observations, step judgements, and score into a new session."""
    if target_session.status == AssessmentStatus.completed:
        raise AssessmentError("目标会话已经完成，不能覆盖缓存结果")

    source_session_id = cached.source_session.id
    observations = (
        db.query(Observation)
        .filter(Observation.session_id == source_session_id)
        .order_by(Observation.timestamp_ms, Observation.id)
        .all()
    )
    for observation in observations:
        db.add(
            Observation(
                session_id=target_session.id,
                timestamp_ms=observation.timestamp_ms,
                hand=observation.hand,
                tool=observation.tool,
                object=observation.object,
                action=observation.action,
                relation=observation.relation,
                confidence=observation.confidence,
                frame_hash=observation.frame_hash,
                payload=copy.deepcopy(observation.payload or {}),
            )
        )

    steps = (
        db.query(StepAssessment)
        .filter(StepAssessment.session_id == source_session_id)
        .order_by(StepAssessment.step_index, StepAssessment.id)
        .all()
    )
    for step in steps:
        db.add(
            StepAssessment(
                session_id=target_session.id,
                step_index=step.step_index,
                step_id=step.step_id,
                judgement=step.judgement,
                start_ms=step.start_ms,
                end_ms=step.end_ms,
                rule_code=step.rule_code,
                reason=step.reason,
                confidence=step.confidence,
                keyframe_ref=step.keyframe_ref,
                observable_check_results=copy.deepcopy(
                    step.observable_check_results or {}
                ),
            )
        )

    source_report = cached.report
    report = ScoreReport(
        session_id=target_session.id,
        total_score=source_report.total_score,
        result=source_report.result,
        dimension_scores=copy.deepcopy(source_report.dimension_scores or {}),
        evidence_coverage=source_report.evidence_coverage,
        model_version=source_report.model_version,
        input_hash=target_session.input_sha256 or "",
        scoring_rules_version=source_report.scoring_rules_version,
    )
    db.add(report)
    now = utcnow()
    target_session.status = AssessmentStatus.completed
    target_session.started_at = now
    target_session.finished_at = now
    db.commit()
    db.refresh(report)
    return report


def create_session(
    db: Session,
    *,
    process_key: str,
    mode: str = "recorded",
    learner_id: str | None = None,
    sop_version_id: str | None = None,
    input_asset_id: str | None = None,
    input_sha256: str | None = None,
) -> AssessmentSession:
    if mode not in ("recorded", "live", "manual_offline"):
        raise AssessmentError(f"不支持的考评模式: {mode}")
    if mode in {"recorded", "live"} and not sop_version_id:
        raise AssessmentError(f"正式 {mode} 考评必须绑定明确的已发布 SOP 版本")
    # 显式绑定 SOP：必须存在且已发布（整改：防 draft/孤儿 SOP 被用于正式考评）。
    if sop_version_id:
        sop_row = db.get(SopVersion, sop_version_id)
        if sop_row is None:
            raise AssessmentError(f"SOP 版本不存在: {sop_version_id}")
        if sop_row.status != SopPublishStatus.published:
            raise AssessmentError(
                f"SOP {sop_version_id} 未发布（{sop_row.status.value}），不能用于考评"
            )
        if sop_row.process_key != process_key:
            raise AssessmentError("SOP 与工序不匹配，请选择同一工序的已发布 SOP")
    session = AssessmentSession(
        mode=mode,
        process_key=process_key,
        learner_id=learner_id,
        sop_version_id=sop_version_id,
        input_asset_id=input_asset_id,
        input_sha256=input_sha256,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _load_sop(
    db: Session,
    sop_version_id: str | None,
    process_key: str,
    *,
    allow_latest: bool = False,
) -> SopSchema:
    if sop_version_id:
        row = db.get(SopVersion, sop_version_id)
        if row is None:
            raise AssessmentError(f"SOP 版本不存在: {sop_version_id}")
        if row.status != SopPublishStatus.published:
            raise AssessmentError(f"SOP 未发布（{row.status.value}），不能用于考评：请先发布该 SOP")
        if row.process_key != process_key:
            raise AssessmentError("会话绑定的 SOP 与工序不匹配，无法处理")
    else:
        if not allow_latest:
            raise AssessmentError("考评会话未绑定明确的 SOP 版本，无法处理；请重新创建会话")
        row = (
            db.query(SopVersion)
            .filter(
                SopVersion.process_key == process_key,
                SopVersion.status == SopPublishStatus.published,
            )
            .order_by(SopVersion.version.desc())
            .first()
        )
        if row is None:
            raise AssessmentError(f"工序 {process_key} 没有已发布的 SOP；请先编译并发布")
    return SopSchema(
        id=row.id,
        process_key=row.process_key,
        version=row.version,
        source_grade=SourceGrade(row.source_grade.value),
        steps=[SopStep.model_validate(s) for s in row.steps],
        scoring_profile=ScoringProfile.model_validate(row.scoring_profile),
        prompt_version=row.prompt_version,
    )


def evaluate(
    db: Session,
    session_id: str,
    *,
    judgements: list[dict[str, Any]],
    model_version: str = MANUAL_REVIEW_MODEL_VERSION,
    recorded_job: Job | None = None,
    evidence_source: str = "manual_review",
) -> ScoreReport:
    """Persist deterministic scoring with explicit report provenance.

    Public/manual callers do not control ``model_version``: without an exact
    recorded worker job the report is always marked ``manual_review``.  The
    worker passes its current job and actual vision model, binding both to the
    report in the commit below.
    """
    session = db.get(AssessmentSession, session_id)
    if session is None:
        raise AssessmentNotFoundError(session_id)
    if session.status == AssessmentStatus.completed:
        raise AssessmentError("会话已评分，不可重复评分")
    if recorded_job is None:
        if session.mode not in {"recorded", "manual_offline"}:
            raise AssessmentError("手工复核只能用于 manual_offline 或旧版 recorded 会话")
        model_version = MANUAL_REVIEW_MODEL_VERSION
        evidence_source = "manual_review"
        active_recorded_job = next(
            (
                candidate
                for candidate in db.query(Job)
                .filter(Job.job_type == "recorded_assessment")
                .order_by(Job.created_at.desc())
                if str((candidate.payload or {}).get("session_id", "")) == session.id
                and candidate.status
                not in (JobStatus.failed, JobStatus.cancelled)
            ),
            None,
        )
        if active_recorded_job is not None:
            raise AssessmentError("自动视频考评任务仍有效，不能写入手工复核报告")
    else:
        if session.mode != "recorded":
            raise AssessmentError("recorded_assessment 任务只能处理 recorded 会话")
        if evidence_source not in {"vision", "fixture"}:
            raise AssessmentError("自动评分报告必须声明真实视觉或演示夹具来源")

    sop = _load_sop(
        db,
        session.sop_version_id,
        session.process_key,
        allow_latest=session.mode == "manual_offline",
    )
    steps = sop.steps

    results: list[StepResult] = []
    for j in judgements:
        step = next((s for s in steps if s.index == j["step_index"]), None)
        if step is None:
            raise AssessmentError(f"步骤 {j['step_index']} 不在 SOP 中")
        observable_check_results = _clean_observable_check_results(
            step,
            j.get("observable_check_results"),
        )
        results.append(
            StepResult(
                step=step,
                judgement=j.get("judgement", "insufficient_evidence"),
                start_ms=j.get("start_ms", 0),
                end_ms=j.get("end_ms", 0),
                rule_code=j.get("rule_code", ""),
                reason=j.get("reason", ""),
                confidence=j.get("confidence", 0.0),
                observable_check_results=observable_check_results,
            )
        )

    result = score_assessment(steps, results, sop.scoring_profile)

    # 落库步骤判定
    for r in results:
        db.add(
            StepAssessment(
                session_id=session.id,
                step_index=r.step.index,
                step_id=r.step.id,
                judgement=StepJudgement(r.judgement),
                start_ms=r.start_ms,
                end_ms=r.end_ms,
                rule_code=r.rule_code,
                reason=r.reason,
                confidence=r.confidence,
                observable_check_results=copy.deepcopy(r.observable_check_results),
                keyframe_ref=next(
                    (
                        str(j["keyframe_ref"])
                        for j in judgements
                        if j.get("step_index") == r.step.index and j.get("keyframe_ref")
                    ),
                    None,
                ),
            )
        )

    report = ScoreReport(
        session_id=session.id,
        total_score=result.total_score,
        result=result.result,
        dimension_scores=result.dimension_scores,
        evidence_coverage=result.evidence_coverage,
        model_version=model_version,
        input_hash=session.input_sha256 or "",
        scoring_rules_version=SCORING_RULES_VERSION,
    )
    db.add(report)
    session.status = AssessmentStatus.completed
    session.finished_at = datetime.now(UTC)
    if recorded_job is not None:
        db.flush()
        _bind_report_to_recorded_job(
            job=recorded_job,
            session=session,
            report=report,
            evidence_source=evidence_source,
        )
    db.commit()
    db.refresh(report)
    return report


def _clean_observable_check_results(
    step: SopStep,
    raw: object,
) -> dict[str, str]:
    """Validate exact visual-check keys and normalize a provided partial map.

    An absent or empty map is retained for backward compatibility with legacy
    manual judgements, where the deterministic aligner's rule code is the only
    persisted visual-evidence signal. Completion checks and safety checks share
    the persisted map but retain their distinct scoring semantics. Once any
    exact result is supplied, every omitted visual check becomes
    ``insufficient_evidence`` rather than inheriting a coarse code.
    """
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict):
        raise AssessmentError(f"步骤 {step.index} 的可观察检查结果必须是对象")

    visual_checks = [*step.completion_checks, *step.observable_checks]
    allowed_checks = set(visual_checks)
    cleaned: dict[str, str] = {}
    for check, status in raw.items():
        if not isinstance(check, str) or check not in allowed_checks:
            raise AssessmentError(f"步骤 {step.index} 包含未声明的视觉检查: {check}")
        if not isinstance(status, str) or status not in _OBSERVABLE_CHECK_STATUSES:
            raise AssessmentError(
                f"步骤 {step.index} 的检查 {check} 状态无效: {status}"
            )
        cleaned[check] = status

    return {
        check: cleaned.get(check, "insufficient_evidence")
        for check in visual_checks
    }


def get_report(db: Session, report_id: str) -> ScoreReport:
    row = db.get(ScoreReport, report_id)
    if row is None:
        raise AssessmentNotFoundError(report_id)
    return row


def run_recorded_assessment_job(
    db: Session,
    job_id: str,
    work_dir: Path,
    *,
    demo: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """预录视频自动评分任务：视频 → 观察 → 对齐 → 评分（整改报告 P0-B）。

    模型缺失/无视频/质量不足 → 结构化拒判落库，不生成伪分数。
    demo 仅在 GONGXU_DEMO_FIXTURES=true 且显式启用时使用 Fixture。
    """
    from gongxu.db.models import Job, Observation
    from gongxu.vision.pipeline import RejectError, VisionPipelineError, run_recorded_pipeline

    job = db.get(Job, job_id)
    if job is None:
        raise AssessmentError(f"任务不存在: {job_id}")
    payload = job.payload or {}
    session_id = payload.get("session_id")
    if not session_id:
        raise AssessmentError("任务缺少 session_id")
    session = db.get(AssessmentSession, session_id)
    if session is None:
        raise AssessmentNotFoundError(session_id)
    if session.mode != "recorded":
        raise AssessmentError("recorded_assessment 任务只能处理 recorded 会话")
    # Historical formal sessions without an immutable SOP binding cannot be
    # processed safely: selecting the latest version would change their result.
    _load_sop(db, session.sop_version_id, session.process_key)

    # Crash recovery trusts only a report transactionally bound to this exact
    # job. A manual review (or an arbitrary completed report) is never enough.
    recovered = _recoverable_recorded_report(db, job=job, session=session)
    if recovered is not None:
        existing_report, recovered_evidence_source = recovered
        recovered_coaching = _coaching_from_job(job)
        if recovered_coaching is None:
            _emit_progress(
                progress_callback,
                "coaching",
                96,
                "正在恢复教学反馈",
            )
            recovered_coaching = _generate_recorded_coaching(
                db,
                job=job,
                session=session,
                report=existing_report,
                evidence_source=recovered_evidence_source,
            )
        _emit_progress(
            progress_callback,
            "done",
            100,
            "已恢复先前完成的考评报告",
            {"report_id": existing_report.id, "recovered": True},
        )
        return {
            "session_id": session_id,
            "report_id": existing_report.id,
            "total_score": existing_report.total_score,
            "result": existing_report.result,
            "evidence_coverage": existing_report.evidence_coverage,
            "observations": db.query(Observation)
            .filter(Observation.session_id == session_id)
            .count(),
            "quality": {},
            "evidence_source": recovered_evidence_source,
            "model_version": existing_report.model_version,
            "scoring_rules_version": existing_report.scoring_rules_version,
            "coaching": recovered_coaching,
            "recovered": True,
        }

    # Remove partial writes left by a failed attempt before a retry.
    db.query(Observation).filter(Observation.session_id == session_id).delete()
    db.query(StepAssessment).filter(StepAssessment.session_id == session_id).delete()
    db.query(ScoreReport).filter(ScoreReport.session_id == session_id).delete()
    session.status = AssessmentStatus.processing
    session.finished_at = None
    db.commit()
    _emit_progress(progress_callback, "preparing", 5, "正在准备考评视频")

    try:
        _emit_progress(
            progress_callback,
            "vision_analyzing",
            20,
            "正在抽帧并识别工具、对象与动作",
        )
        inference_started = time.monotonic()

        def inference_progress(analyzed_frames: int, total_frames: int) -> None:
            elapsed = max(time.monotonic() - inference_started, 0.001)
            remaining = max(0, total_frames - analyzed_frames)
            estimated_remaining = round(remaining / max(analyzed_frames / elapsed, 0.001))
            fraction = analyzed_frames / max(total_frames, 1)
            _emit_progress(
                progress_callback,
                "vision_analyzing",
                20 + round(fraction * 50),
                f"正在识别动作与工具（{analyzed_frames}/{total_frames} 帧）",
                {
                    "analyzed_frames": analyzed_frames,
                    "total_frames": total_frames,
                    "estimated_remaining_seconds": estimated_remaining,
                },
            )

        result = run_recorded_pipeline(
            db,
            session,
            work_dir=work_dir,
            demo=demo,
            inference_progress=inference_progress,
        )
    except RejectError as exc:
        # 结构化拒判：落库 inconclusive 状态，不生成数值报告。
        # model_version 如实记录实际推理适配器（qwen/onnx），避免误标 no-vision-adapter。
        from gongxu.vision.pipeline import _model_version

        if exc.reason_code == "provider_export_not_authorized":
            # Consent rejection must not instantiate the external adapter just
            # to obtain its label.  The configured model name is sufficient and
            # makes the non-invocation explicit in the persisted report.
            from gongxu.config import get_settings

            configured_model = get_settings().llm_model or "unconfigured"
            rejected_model_version = f"qwen@{configured_model}:not-invoked"
        else:
            try:
                rejected_sop = _load_sop(db, session.sop_version_id, session.process_key)
                rejected_sop_steps = rejected_sop.steps
            except AssessmentError:
                rejected_sop_steps = None
            rejected_model_version = _model_version(
                db,
                process_key=session.process_key,
                sop_steps=rejected_sop_steps,
            )
        session.status = AssessmentStatus.completed
        session.finished_at = datetime.now(UTC)
        rejected_report = ScoreReport(
            session_id=session.id,
            total_score=0.0,
            result="inconclusive",
            dimension_scores={},
            evidence_coverage=0.0,
            model_version=rejected_model_version,
            input_hash=session.input_sha256 or "",
            scoring_rules_version=SCORING_RULES_VERSION,
        )
        db.add(rejected_report)
        db.flush()
        _bind_report_to_recorded_job(
            job=job,
            session=session,
            report=rejected_report,
            evidence_source="vision_rejection",
        )
        db.commit()
        report = rejected_report
        _emit_progress(
            progress_callback,
            "coaching",
            96,
            "正在生成证据不足后的复练建议",
        )
        coaching = _generate_recorded_coaching(
            db,
            job=job,
            session=session,
            report=report,
            evidence_source="vision_rejection",
        )
        _emit_progress(
            progress_callback,
            "done",
            100,
            "证据不足，已生成无法判断报告",
            {"report_id": report.id if report else None, "reason_code": exc.reason_code},
        )
        return {
            "session_id": session_id,
            "report_id": report.id if report else None,
            "rejected": True,
            "status": "inconclusive",
            "reason_code": exc.reason_code,
            "reason": exc.reason,
            "missing_models": exc.missing_models,
            "model_version": rejected_model_version,
            "provider_metrics": exc.provider_metrics,
            "scoring_rules_version": SCORING_RULES_VERSION,
            "evidence_source": "vision_rejection",
            "coaching": coaching,
        }
    except VisionPipelineError as exc:
        session.status = AssessmentStatus.failed
        db.commit()
        raise AssessmentError(f"预录考评失败: {exc}") from exc

    _emit_progress(
        progress_callback,
        "aligning",
        78,
        "正在合并事件并与 SOP 步骤对齐",
        {
            "observation_count": int(result.get("observations", 0)),
            "quality": result.get("quality", {}),
        },
    )
    _emit_progress(progress_callback, "scoring", 90, "正在生成步骤级评分与证据报告")
    report = evaluate(
        db,
        session_id,
        judgements=result["judgements"],
        model_version=result.get("model_version", "no-vision-adapter"),
        recorded_job=job,
        evidence_source=result.get("evidence_source", "vision"),
    )
    _emit_progress(
        progress_callback,
        "coaching",
        96,
        "正在生成不参与改分的教学反馈",
    )
    coaching = _generate_recorded_coaching(
        db,
        job=job,
        session=session,
        report=report,
        evidence_source=result.get("evidence_source", "vision"),
    )
    _emit_progress(
        progress_callback,
        "done",
        100,
        "考评报告已生成",
        {"report_id": report.id, "result": report.result},
    )
    return {
        "session_id": session_id,
        "report_id": report.id,
        "total_score": report.total_score,
        "result": report.result,
        "evidence_coverage": report.evidence_coverage,
        "observations": result["observations"],
        "quality": result["quality"],
        "evidence_source": result.get("evidence_source", "vision"),
        "model_version": result.get("model_version", "no-vision-adapter"),
        "provider_metrics": result.get("provider_metrics", {}),
        "scoring_rules_version": report.scoring_rules_version,
        "coaching": coaching,
    }
