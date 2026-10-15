"""在明确授权的视频集上运行 Qwen 预录考评并生成可追溯报告。

这个模块只编排现有产品流水线，不训练本地模型，也不从公开数据集推断外发
许可。整份清单必须先通过授权、SHA256、SOP 与运行配置预检，之后才可能
产生任何外部 Provider 调用。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from gongxu.assessment import service as assessment_service
from gongxu.assets import service as asset_service
from gongxu.config import get_settings
from gongxu.data.manifest import sha256_file
from gongxu.db.models import (
    AssessmentSession,
    AssessmentStatus,
    AuditAction,
    AuditLog,
    Job,
    JobStatus,
    SopPublishStatus,
    SopVersion,
    StepAssessment,
    User,
    UserRole,
    utcnow,
)
from gongxu.db.session import get_session_factory, init_db
from gongxu.jobs import service as jobs_service
from gongxu.security.audit import record_audit

_SCHEMA_VERSION = "qwen-authorized-evaluation-v1"
_EVALUATION_CONTRACT = "qwen-evaluation-v19"
_AUTHORIZATION_SCOPE = "team_owned_explicit_provider_processing"
_PUBLIC_DATASET_SCOPE = "official_public_dataset_license_plus_operator_acknowledgement"
_JUDGEMENTS = ("passed", "failed", "unobservable", "insufficient_evidence")


class EvaluationManifestError(ValueError):
    """评测清单或运行环境不满足外发门禁。"""


class ExpectedStep(BaseModel):
    step_index: int = Field(ge=1)
    judgement: Literal[
        "passed",
        "failed",
        "unobservable",
        "insufficient_evidence",
    ]


class EvaluationCase(BaseModel):
    case_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    video_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    authorization_note: str = Field(min_length=8, max_length=500)
    expected_result: Literal["pass", "fail", "inconclusive"]
    expected_steps: list[ExpectedStep] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_step_indexes(self) -> EvaluationCase:
        indexes = [item.step_index for item in self.expected_steps]
        if len(indexes) != len(set(indexes)):
            raise ValueError("expected_steps 中存在重复 step_index")
        return self


class EvaluationManifest(BaseModel):
    schema_version: Literal["qwen-authorized-evaluation-v1"] = "qwen-authorized-evaluation-v1"
    dataset_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    evaluation_role: Literal["development_validation", "frozen_test"]
    provider_export_authorized: Literal[True]
    authorization_scope: Literal[
        "team_owned_explicit_provider_processing",
        "official_public_dataset_license_plus_operator_acknowledgement",
    ] = "team_owned_explicit_provider_processing"
    authorization_note: str = Field(min_length=8, max_length=500)
    official_source_url: str | None = None
    license_id: str | None = None
    operator_acknowledged_provider_processing: bool = False
    authorized_by_username: str = Field(min_length=1, max_length=64)
    process_key: str = Field(min_length=1, max_length=128)
    sop_version_id: str = Field(min_length=1, max_length=32)
    frozen_at: str | None = None
    threshold_tuning_prohibited: bool = True
    cases: list[EvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_batch(self) -> EvaluationManifest:
        case_ids = [item.case_id for item in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("cases 中存在重复 case_id")
        if self.evaluation_role == "frozen_test":
            if not self.frozen_at:
                raise ValueError("frozen_test 必须填写 frozen_at")
            if not self.threshold_tuning_prohibited:
                raise ValueError("frozen_test 必须禁止使用测试结果调参")
        if self.authorization_scope == _PUBLIC_DATASET_SCOPE:
            if not self.official_source_url or not self.license_id:
                raise ValueError("公开数据集外发必须记录官方来源 URL 与许可标识")
            if not self.operator_acknowledged_provider_processing:
                raise ValueError("公开数据集外发必须由操作人明确确认第三方处理")
        return self


class PreflightCase(BaseModel):
    case_id: str
    path: Path
    filename: str
    sha256: str
    expected_result: str
    expected_steps: dict[int, str]
    authorization_note: str


class PreflightResult(BaseModel):
    manifest_path: Path
    manifest_sha256: str
    manifest: EvaluationManifest
    authorizer_id: str
    sop_step_indexes: list[int]
    cases: list[PreflightCase]


def _load_manifest(path: Path) -> EvaluationManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvaluationManifestError(f"评测清单不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationManifestError(f"评测清单不是有效 JSON: {exc}") from exc
    try:
        return EvaluationManifest.model_validate(payload)
    except ValidationError as exc:
        raise EvaluationManifestError(f"评测清单校验失败:\n{exc}") from exc


def _validate_runtime() -> None:
    settings = get_settings()
    problems: list[str] = []
    if settings.llm_mode != "live":
        problems.append("GONGXU_LLM_MODE 必须为 live")
    if settings.vision_backend != "qwen":
        problems.append("GONGXU_VISION_BACKEND 必须为 qwen")
    if not settings.llm_base_url:
        problems.append("缺少 GONGXU_LLM_BASE_URL")
    if not settings.llm_api_key:
        problems.append("缺少 GONGXU_LLM_API_KEY")
    if not settings.llm_model:
        problems.append("缺少 GONGXU_LLM_MODEL")
    if problems:
        raise EvaluationManifestError("Qwen 运行配置未就绪: " + "；".join(problems))


def preflight_manifest(db: Session, manifest_path: Path) -> PreflightResult:
    """完整预检；调用成功之前不会构造 Provider 或读取视频帧。"""
    manifest_path = manifest_path.expanduser().resolve()
    manifest = _load_manifest(manifest_path)
    _validate_runtime()

    authorizer = db.scalar(select(User).where(User.username == manifest.authorized_by_username))
    if authorizer is None or not authorizer.is_active:
        raise EvaluationManifestError("授权登记人账号不存在或已停用")
    if authorizer.role not in (UserRole.admin, UserRole.instructor):
        raise EvaluationManifestError("授权登记人必须是 admin 或 instructor")

    sop = db.get(SopVersion, manifest.sop_version_id)
    if sop is None:
        raise EvaluationManifestError(f"SOP 版本不存在: {manifest.sop_version_id}")
    if sop.status != SopPublishStatus.published:
        raise EvaluationManifestError("评测只能绑定已发布 SOP")
    if sop.process_key != manifest.process_key:
        raise EvaluationManifestError("清单 process_key 与 SOP 不一致")

    sop_indexes = sorted(int(step["index"]) for step in sop.steps)
    expected_index_set = set(sop_indexes)
    resolved_cases: list[PreflightCase] = []
    for item in manifest.cases:
        path = Path(item.video_path).expanduser()
        if not path.is_absolute():
            path = manifest_path.parent / path
        path = path.resolve()
        if not path.is_file():
            raise EvaluationManifestError(f"案例 {item.case_id} 视频不存在: {path}")
        actual_hash = sha256_file(path)
        if actual_hash.lower() != item.sha256.lower():
            raise EvaluationManifestError(
                f"案例 {item.case_id} SHA256 不符；为避免发送错误文件，已终止整批评测"
            )
        labels: dict[int, str] = {step.step_index: step.judgement for step in item.expected_steps}
        if set(labels) != expected_index_set:
            missing = sorted(expected_index_set - set(labels))
            extra = sorted(set(labels) - expected_index_set)
            raise EvaluationManifestError(
                f"案例 {item.case_id} 必须标注 SOP 全部步骤；缺少={missing}，多余={extra}"
            )
        resolved_cases.append(
            PreflightCase(
                case_id=item.case_id,
                path=path,
                filename=path.name,
                sha256=actual_hash.lower(),
                expected_result=item.expected_result,
                expected_steps=labels,
                authorization_note=item.authorization_note,
            )
        )

    return PreflightResult(
        manifest_path=manifest_path,
        manifest_sha256=sha256_file(manifest_path),
        manifest=manifest,
        authorizer_id=authorizer.id,
        sop_step_indexes=sop_indexes,
        cases=resolved_cases,
    )


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


def calculate_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """从逐案例归档结果计算准确率；运行失败不伪装成拒判。"""
    completed = [case for case in cases if case.get("execution_status") == "completed"]
    result_correct = sum(
        case.get("predicted_result") == case.get("expected_result") for case in completed
    )
    decision_cases = [case for case in completed if case.get("expected_result") in ("pass", "fail")]
    decision_correct = sum(
        case.get("predicted_result") == case.get("expected_result") for case in decision_cases
    )
    confusion = {
        expected: {predicted: 0 for predicted in ("pass", "fail", "inconclusive")}
        for expected in ("pass", "fail", "inconclusive")
    }
    step_confusion = {
        expected: {predicted: 0 for predicted in _JUDGEMENTS} for expected in _JUDGEMENTS
    }
    for case in completed:
        expected_result = str(case["expected_result"])
        predicted_result = str(case["predicted_result"])
        if expected_result in confusion and predicted_result in confusion[expected_result]:
            confusion[expected_result][predicted_result] += 1
        predicted_steps = {
            int(key): value for key, value in case.get("predicted_steps", {}).items()
        }
        for raw_index, expected in case.get("expected_steps", {}).items():
            predicted = predicted_steps.get(int(raw_index), "insufficient_evidence")
            if expected in step_confusion and predicted in step_confusion[expected]:
                step_confusion[expected][predicted] += 1

    per_class: dict[str, dict[str, float | int]] = {}
    all_label_f1_values: list[float] = []
    decision_f1_values: list[float] = []
    for label in _JUDGEMENTS:
        tp = step_confusion[label][label]
        fp = sum(step_confusion[other][label] for other in _JUDGEMENTS if other != label)
        fn = sum(step_confusion[label][other] for other in _JUDGEMENTS if other != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        support = sum(step_confusion[label].values())
        if support:
            all_label_f1_values.append(f1)
            if label in ("passed", "failed"):
                decision_f1_values.append(f1)
        per_class[label] = {
            "support": support,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        }

    latencies = [float(case["latency_seconds"]) for case in completed]
    calls = sum(int(case.get("provider_metrics", {}).get("calls", 0)) for case in completed)
    failed_calls = sum(
        int(case.get("provider_metrics", {}).get("failed_calls", 0)) for case in completed
    )
    tokens = sum(int(case.get("provider_metrics", {}).get("total_tokens", 0)) for case in completed)
    rejected = sum(case.get("predicted_result") == "inconclusive" for case in completed)
    return {
        "case_count": len(cases),
        "completed_count": len(completed),
        "execution_failure_count": len(cases) - len(completed),
        "result_accuracy": (
            round(decision_correct / len(decision_cases), 4) if decision_cases else None
        ),
        "overall_three_state_accuracy": (
            round(result_correct / len(completed), 4) if completed else None
        ),
        "rejection_rate": round(rejected / len(completed), 4) if completed else None,
        "result_confusion_matrix": confusion,
        "step_macro_f1": (
            round(statistics.fmean(decision_f1_values), 4) if decision_f1_values else None
        ),
        "step_all_label_macro_f1": (
            round(statistics.fmean(all_label_f1_values), 4) if all_label_f1_values else None
        ),
        "step_per_class": per_class,
        "step_confusion_matrix": step_confusion,
        "latency_seconds_p50": _percentile(latencies, 0.5),
        "latency_seconds_p95": _percentile(latencies, 0.95),
        "provider_calls": calls,
        "provider_failed_calls": failed_calls,
        "provider_total_tokens": tokens,
        "pricing_status": "not_configured",
    }


def _case_result_from_db(
    db: Session,
    *,
    case: PreflightCase,
    session_id: str,
    job_result: dict[str, Any],
    latency_seconds: float,
) -> dict[str, Any]:
    step_rows = list(
        db.scalars(
            select(StepAssessment)
            .where(StepAssessment.session_id == session_id)
            .order_by(StepAssessment.step_index)
        )
    )
    predicted_result = str(job_result.get("result") or job_result.get("status") or "inconclusive")
    return {
        "case_id": case.case_id,
        "filename": case.filename,
        "sha256": case.sha256,
        "execution_status": "completed",
        "expected_result": case.expected_result,
        "predicted_result": predicted_result,
        "expected_steps": {str(key): value for key, value in case.expected_steps.items()},
        "predicted_steps": {str(row.step_index): row.judgement.value for row in step_rows},
        "session_id": session_id,
        "report_id": job_result.get("report_id"),
        "rejected": bool(job_result.get("rejected", False)),
        "reason_code": job_result.get("reason_code"),
        "reason": job_result.get("reason"),
        "evidence_coverage": job_result.get("evidence_coverage", 0.0),
        "model_version": job_result.get("model_version", ""),
        "provider_metrics": job_result.get("provider_metrics", {}),
        "latency_seconds": round(latency_seconds, 3),
    }


def _audit_provider_result(
    db: Session,
    *,
    preflight: PreflightResult,
    case: PreflightCase,
    job: Job,
    archived: dict[str, Any],
    recovered: bool = False,
) -> None:
    provider_metrics = archived.get("provider_metrics", {})
    if not isinstance(provider_metrics, dict) or provider_metrics.get("provider") != "qwen":
        return
    existing = db.scalar(
        select(AuditLog).where(
            AuditLog.action == AuditAction.provider_call,
            AuditLog.target_type == "qwen_evaluation_case",
            AuditLog.target_id == job.id,
        )
    )
    if existing is not None:
        return
    record_audit(
        db,
        action=AuditAction.provider_call,
        actor_id=preflight.authorizer_id,
        target_type="qwen_evaluation_case",
        target_id=job.id,
        detail={
            "dataset_id": preflight.manifest.dataset_id,
            "case_id": case.case_id,
            "model_id": provider_metrics.get("model_id", ""),
            "calls": provider_metrics.get("calls", 0),
            "failed_calls": provider_metrics.get("failed_calls", 0),
            "total_tokens": provider_metrics.get("total_tokens", 0),
            "result": archived.get("predicted_result"),
            "recovered": recovered,
        },
    )


def _execute_case(db: Session, preflight: PreflightResult, case: PreflightCase) -> dict[str, Any]:
    settings = get_settings()
    from gongxu.vision.pipeline import _model_version

    sop = assessment_service._load_sop(
        db,
        preflight.manifest.sop_version_id,
        preflight.manifest.process_key,
    )
    model_contract = _model_version(
        db,
        process_key=preflight.manifest.process_key,
        sop_steps=sop.steps,
    )
    identity = {
        "dataset_id": preflight.manifest.dataset_id,
        "case_id": case.case_id,
        "sha256": case.sha256,
        "sop_version_id": preflight.manifest.sop_version_id,
        "model": settings.llm_model,
        "model_contract": model_contract,
        "evaluation_contract": _EVALUATION_CONTRACT,
    }
    identity_digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    idempotency_key = f"qwen-eval:{identity_digest}"
    job = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
    session: AssessmentSession | None = None
    if job is not None:
        payload = job.payload or {}
        if any(str(payload.get(key, "")) != str(value) for key, value in identity.items()):
            raise RuntimeError("评测任务身份摘要冲突，已拒绝复用")
        session_id = str(payload.get("session_id", ""))
        session = db.get(AssessmentSession, session_id)
        if session is None:
            raise RuntimeError("既有评测任务绑定的会话不存在")
        if job.status == JobStatus.succeeded and isinstance(job.result, dict):
            elapsed = 0.0
            if job.started_at is not None and job.finished_at is not None:
                elapsed = max((job.finished_at - job.started_at).total_seconds(), 0.0)
            recovered = _case_result_from_db(
                db,
                case=case,
                session_id=session.id,
                job_result=job.result,
                latency_seconds=elapsed,
            )
            recovered["recovered_from_job"] = True
            _audit_provider_result(
                db,
                preflight=preflight,
                case=case,
                job=job,
                archived=recovered,
                recovered=True,
            )
            return recovered

    if session is None:
        asset = asset_service.register_asset(
            db,
            kind="video",
            filename=case.filename,
            temp_path=case.path,
            license_note=(
                f"Qwen授权评测/{preflight.manifest.dataset_id}: "
                f"{preflight.manifest.authorization_note}; {case.authorization_note}"
            )[:255],
            authorized_by=preflight.authorizer_id,
            sha256=case.sha256,
            allow_provider_export=True,
        )
        record_audit(
            db,
            action=AuditAction.asset_upload,
            actor_id=preflight.authorizer_id,
            target_type="evaluation_asset",
            target_id=asset.id,
            detail={
                "dataset_id": preflight.manifest.dataset_id,
                "case_id": case.case_id,
                "sha256": case.sha256,
                "provider_export_authorized": True,
            },
        )
        session = assessment_service.create_session(
            db,
            process_key=preflight.manifest.process_key,
            mode="recorded",
            sop_version_id=preflight.manifest.sop_version_id,
            input_asset_id=asset.id,
            input_sha256=case.sha256,
        )
        now = utcnow()
        job = Job(
            job_type="recorded_assessment",
            payload={"session_id": session.id, "requested_by": preflight.authorizer_id, **identity},
            idempotency_key=idempotency_key,
            status=JobStatus.running,
            max_retries=0,
            lease_owner="qwen-evaluator",
            lease_expires_at=now + timedelta(hours=1),
            heartbeat_at=now,
            started_at=now,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
    else:
        if job is None:
            raise RuntimeError("既有评测会话缺少任务记录")
        now = utcnow()
        job.status = JobStatus.running
        job.lease_owner = "qwen-evaluator"
        job.lease_expires_at = now + timedelta(hours=1)
        job.heartbeat_at = now
        job.finished_at = None
        if job.started_at is None:
            job.started_at = now
        db.commit()

    if job is None:
        raise RuntimeError("评测任务创建失败")
    session.status = AssessmentStatus.processing
    db.commit()

    work_root = settings.data_root / "runtime" / "qwen-evaluation"
    work_root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix=f"{case.case_id}_", dir=work_root) as tmpdir:
            result = assessment_service.run_recorded_assessment_job(
                db,
                job.id,
                work_dir=Path(tmpdir),
            )
        jobs_service.complete(db, job.id, result=result)
        archived = _case_result_from_db(
            db,
            case=case,
            session_id=session.id,
            job_result=result,
            latency_seconds=time.monotonic() - started,
        )
        _audit_provider_result(
            db,
            preflight=preflight,
            case=case,
            job=job,
            archived=archived,
        )
        return archived
    except Exception as exc:
        db.rollback()
        jobs_service.fail(db, job.id, error=f"{type(exc).__name__}: {exc}")
        return {
            "case_id": case.case_id,
            "filename": case.filename,
            "sha256": case.sha256,
            "execution_status": "failed",
            "expected_result": case.expected_result,
            "expected_steps": {str(key): value for key, value in case.expected_steps.items()},
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "latency_seconds": round(time.monotonic() - started, 3),
        }


def _report_payload(
    preflight: PreflightResult,
    cases: list[dict[str, Any]],
    *,
    complete: bool,
) -> dict[str, Any]:
    settings = get_settings()
    return {
        "schema_version": _SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "complete": complete,
        "evaluation_scope": "Qwen external multimodal API on explicitly authorized videos",
        "not_a_claim": (
            "This report is not local-model training or industrial safety certification."
        ),
        "dataset": {
            "dataset_id": preflight.manifest.dataset_id,
            "evaluation_role": preflight.manifest.evaluation_role,
            "manifest_sha256": preflight.manifest_sha256,
            "provider_export_authorized": True,
            "authorization_scope": preflight.manifest.authorization_scope,
            "official_source_url": preflight.manifest.official_source_url,
            "license_id": preflight.manifest.license_id,
            "operator_acknowledged_provider_processing": (
                preflight.manifest.operator_acknowledged_provider_processing
            ),
            "frozen_at": preflight.manifest.frozen_at,
            "threshold_tuning_prohibited": preflight.manifest.threshold_tuning_prohibited,
        },
        "runtime": {
            "provider": "qwen",
            "model": settings.llm_model,
            "evaluation_contract": _EVALUATION_CONTRACT,
            "base_url_configured": bool(settings.llm_base_url),
            "api_key_configured": bool(settings.llm_api_key),
            "api_key": "not_recorded",
            "process_key": preflight.manifest.process_key,
            "sop_version_id": preflight.manifest.sop_version_id,
        },
        "metrics": calculate_metrics(cases),
        "cases": cases,
    }


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    metrics = payload["metrics"]
    rows = []
    for case in payload["cases"]:
        rows.append(
            "| {case_id} | {status} | {expected} | {predicted} | {latency} |".format(
                case_id=case["case_id"],
                status=case["execution_status"],
                expected=case.get("expected_result", "-"),
                predicted=case.get("predicted_result", "-"),
                latency=case.get("latency_seconds", "-"),
            )
        )
    text = f"""# Qwen 授权视频评测报告

- 数据集：`{payload["dataset"]["dataset_id"]}`
- 评测角色：`{payload["dataset"]["evaluation_role"]}`
- 模型：`{payload["runtime"]["model"]}`
- 清单 SHA256：`{payload["dataset"]["manifest_sha256"]}`
- 流程完整：`{payload["complete"]}`

## 指标

- 完成案例：{metrics["completed_count"]} / {metrics["case_count"]}
- 执行失败：{metrics["execution_failure_count"]}
- 通过/未通过结果准确率：{metrics["result_accuracy"]}
- 三状态总体匹配率：{metrics["overall_three_state_accuracy"]}
- 通过/未通过步骤宏 F1：{metrics["step_macro_f1"]}
- 全标签步骤宏 F1：{metrics["step_all_label_macro_f1"]}
- 拒判率：{metrics["rejection_rate"]}
- 延迟 p50 / p95：{metrics["latency_seconds_p50"]} / {metrics["latency_seconds_p95"]} 秒
- Qwen 调用 / 失败：{metrics["provider_calls"]} / {metrics["provider_failed_calls"]}
- Token 总量：{metrics["provider_total_tokens"]}（费用未配置，不估算成本）

## 案例

| 案例 | 执行 | 预期 | 预测 | 耗时（秒） |
|---|---|---|---|---:|
{chr(10).join(rows)}

## 结论边界

本报告只衡量外部 Qwen 多模态 API 在清单所列、明确授权视频上的工程效果，
不代表本地模型训练成果、任意工业工序泛化能力或工业安全认证。`frozen_test`
结果不得用于修改提示词、SOP、评分规则或选择模型。
"""
    path.write_text(text, encoding="utf-8")


def run_evaluation(
    db: Session,
    preflight: PreflightResult,
    *,
    output_path: Path,
    max_cases: int | None = None,
) -> dict[str, Any]:
    """逐案例执行并在每例后原子保存报告，以支持进程中断后继续。"""
    existing_cases: list[dict[str, Any]] = []
    if output_path.exists():
        try:
            previous = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvaluationManifestError(f"已有检查点不可读取: {exc}") from exc
        previous_dataset = previous.get("dataset", {})
        previous_runtime = previous.get("runtime", {})
        if (
            previous_dataset.get("manifest_sha256") != preflight.manifest_sha256
            or previous_runtime.get("model") != get_settings().llm_model
            or previous_runtime.get("sop_version_id") != preflight.manifest.sop_version_id
            or previous_runtime.get("evaluation_contract") != _EVALUATION_CONTRACT
        ):
            raise EvaluationManifestError("已有检查点与当前清单、模型或 SOP 不一致")
        existing_cases = list(previous.get("cases", []))

    completed_keys = {
        (str(case.get("case_id")), str(case.get("sha256")))
        for case in existing_cases
        if case.get("execution_status") == "completed"
    }
    selected = [
        case for case in preflight.cases if (case.case_id, case.sha256) not in completed_keys
    ]
    if max_cases is not None:
        selected = selected[:max_cases]

    # 工程失败不是模型拒判：下次启动时自动重试并替换旧失败记录。
    all_cases = [case for case in existing_cases if case.get("execution_status") == "completed"]
    for case in selected:
        all_cases.append(_execute_case(db, preflight, case))
        _write_report(output_path, _report_payload(preflight, all_cases, complete=False))

    expected_keys = {(case.case_id, case.sha256) for case in preflight.cases}
    recorded_keys = {
        (str(case.get("case_id")), str(case.get("sha256")))
        for case in all_cases
        if case.get("execution_status") == "completed"
    }
    complete = expected_keys == recorded_keys
    payload = _report_payload(preflight, all_cases, complete=complete)
    _write_report(output_path, payload)
    _write_markdown(output_path.with_suffix(".md"), payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行 Qwen 授权视频评测")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--max-cases", type=int, default=None)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        init_db()
        db = get_session_factory()()
        try:
            preflight = preflight_manifest(db, args.manifest)
            default_output = (
                Path("../reports/generated")
                / f"qwen-evaluation-{preflight.manifest.dataset_id}.json"
            )
            output = (args.out or default_output).expanduser().resolve()
            if args.preflight_only:
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "manifest_sha256": preflight.manifest_sha256,
                            "dataset_id": preflight.manifest.dataset_id,
                            "case_count": len(preflight.cases),
                            "provider_calls": 0,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return
            payload = run_evaluation(
                db,
                preflight,
                output_path=output,
                max_cases=args.max_cases,
            )
        finally:
            db.close()
    except (EvaluationManifestError, OSError, RuntimeError) as exc:
        print(f"Qwen 评测失败: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    print(json.dumps(payload["metrics"], ensure_ascii=False, indent=2))
    if not payload["complete"] or payload["metrics"]["execution_failure_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
