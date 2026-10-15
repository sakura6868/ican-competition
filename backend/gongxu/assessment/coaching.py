"""Post-score coaching generation.

The deterministic engine owns every numeric result.  This module only turns
already-persisted step judgements into learner-facing language and a practice
plan.  External calls are optional, consent-gated, and never receive video or
image bytes.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.db.models import ProviderReceipt as ProviderReceiptRow
from gongxu.providers.base import (
    MultimodalProvider,
    ProviderError,
    ProviderReceipt,
)
from gongxu.providers.factory import get_provider

COACHING_VERSION = "coaching-v1"
_MAX_EXPLANATION_CHARS = 1800
_MAX_NOTE_CHARS = 800


def _text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _focus_steps(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item, 64) for item in value[:5] if _text(item, 64)]


def _fallback(step_results: list[dict[str, Any]], report: dict[str, Any]) -> dict[str, Any]:
    priority = [
        step
        for step in step_results
        if str(step.get("judgement", ""))
        in {"failed", "insufficient_evidence", "unobservable"}
    ][:3]
    if str(report.get("result", "")) == "inconclusive":
        explanation = "本次关键步骤证据不足，当前分数仅供参考；请先调整机位、光线或遮挡后重新提交。"
    elif priority:
        details = "；".join(
            f"步骤 {step.get('step_index', '?')}："
            f"{_text(step.get('reason'), 180) or '需按 SOP 复练'}"
            for step in priority
        )
        explanation = f"确定性评分发现以下优先项：{details}。"
    else:
        explanation = "本次可见步骤符合当前 SOP，建议保持操作顺序、工具选择和拍摄视角。"
    return {
        "version": COACHING_VERSION,
        "source": "deterministic_rules",
        "explanation": explanation,
        "practice_plan": {
            "focus_steps": [
                str(step.get("step_id") or step.get("step_index")) for step in priority
            ],
            "repeat_count": 3 if priority else 1,
            "note": "逐项复练后重新提交完整视频；系统只根据可见证据给出反馈。",
        },
        "provider": None,
        "model_id": None,
        "receipt_ids": [],
        "errors": [],
    }


def _persist_receipt(db: Session, receipt: ProviderReceipt) -> str:
    row = ProviderReceiptRow(
        provider=receipt.provider,
        mode=receipt.mode,
        model_id=receipt.model_id,
        prompt_version=receipt.prompt_version,
        input_digest=receipt.input_digest,
        response_digest=receipt.response_digest,
        input_tokens=receipt.input_tokens,
        output_tokens=receipt.output_tokens,
        latency_ms=receipt.latency_ms,
        cost_usd=receipt.cost_usd,
        error=receipt.error,
    )
    db.add(row)
    db.flush()
    return row.id


def generate_coaching(
    db: Session,
    *,
    step_results: list[dict[str, Any]],
    report: dict[str, Any],
    external_allowed: bool,
    provider: MultimodalProvider | None = None,
) -> dict[str, Any]:
    """Generate bounded coaching without changing any score field."""
    coaching = _fallback(step_results, report)
    settings = get_settings()
    if not external_allowed or settings.llm_mode != "live":
        return coaching

    try:
        active_provider = provider or get_provider()
    except ProviderError:
        coaching["errors"] = ["provider_unavailable"]
        return coaching

    receipts: list[str] = []
    receipt_models: list[str] = []
    errors: list[str] = []
    external_explanation = ""
    external_plan: dict[str, Any] = {}
    evidence = {
        "result": str(report.get("result", "inconclusive")),
        "evidence_coverage": float(report.get("evidence_coverage", 0.0)),
        "dimension_scores": dict(report.get("dimension_scores") or {}),
        "instruction": "只解释既有判定，不修改或推测任何数值分数。",
    }
    try:
        external_explanation, receipt = active_provider.explain_assessment(
            step_results, evidence
        )
        receipts.append(_persist_receipt(db, receipt))
        receipt_models.append(receipt.model_id)
    except (ProviderError, TypeError, ValueError):
        errors.append("explanation_unavailable")

    try:
        external_plan, receipt = active_provider.generate_practice_plan(
            {
                **evidence,
                "steps": step_results,
                "instruction": "只生成复练安排，不返回或修改评分。",
            }
        )
        receipts.append(_persist_receipt(db, receipt))
        receipt_models.append(receipt.model_id)
    except (ProviderError, TypeError, ValueError):
        errors.append("practice_plan_unavailable")

    explanation = _text(external_explanation, _MAX_EXPLANATION_CHARS)
    focus_steps = _focus_steps(external_plan.get("focus_steps"))
    try:
        repeat_count = max(1, min(int(external_plan.get("repeat_count", 3)), 10))
    except (TypeError, ValueError):
        repeat_count = 3
    note = _text(external_plan.get("note"), _MAX_NOTE_CHARS)

    if explanation:
        coaching["explanation"] = explanation
    if focus_steps or note:
        coaching["practice_plan"] = {
            "focus_steps": focus_steps,
            "repeat_count": repeat_count,
            "note": note or coaching["practice_plan"]["note"],
        }
    if receipts:
        coaching["source"] = (
            "external_model" if len(receipts) == 2 and not errors else "external_model_partial"
        )
        coaching["provider"] = active_provider.name
        coaching["model_id"] = receipt_models[-1]
        coaching["receipt_ids"] = receipts
    coaching["errors"] = errors
    return coaching


__all__ = ["COACHING_VERSION", "generate_coaching"]
