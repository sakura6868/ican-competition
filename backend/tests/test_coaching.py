from __future__ import annotations

from typing import Any

from gongxu.assessment.coaching import generate_coaching
from gongxu.db.models import ProviderReceipt as ProviderReceiptRow
from gongxu.providers.base import MultimodalProvider, ProviderReceipt, ReferenceSegment


class _CoachingProvider(MultimodalProvider):
    name = "coaching-test"
    mode = "live"

    def probe(self) -> dict[str, Any]:
        return {"ok": True}

    def analyze_reference_segment(
        self, segment: ReferenceSegment
    ) -> tuple[dict[str, Any], ProviderReceipt]:
        raise NotImplementedError

    def compile_sop(
        self, analysis: dict[str, Any], rag_chunks: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], ProviderReceipt]:
        raise NotImplementedError

    def explain_assessment(
        self, step_results: list[dict[str, Any]], evidence: dict[str, Any]
    ) -> tuple[str, ProviderReceipt]:
        assert evidence["result"] == "fail"
        assert "total_score" not in evidence
        return "先复练工具选择，再复练动作顺序。", self._receipt("explain")

    def generate_practice_plan(
        self, report: dict[str, Any]
    ) -> tuple[dict[str, Any], ProviderReceipt]:
        return {
            "focus_steps": ["step-2"],
            "repeat_count": 4,
            "note": "完成四次无错操作后再考评。",
            "total_score": 100,
        }, self._receipt("practice")

    def _receipt(self, suffix: str) -> ProviderReceipt:
        return ProviderReceipt(
            provider=self.name,
            mode=self.mode,
            model_id="coaching-model",
            prompt_version="coaching-test-v1",
            input_digest=f"input-{suffix}",
            response_digest=f"output-{suffix}",
        )


def _steps() -> list[dict[str, Any]]:
    return [
        {
            "step_index": 2,
            "step_id": "step-2",
            "judgement": "failed",
            "reason": "工具选择错误",
        }
    ]


def _report() -> dict[str, Any]:
    return {
        "total_score": 38,
        "result": "fail",
        "evidence_coverage": 1.0,
        "dimension_scores": {"completeness": 60},
    }


def test_coaching_stays_deterministic_without_external_consent(db) -> None:
    result = generate_coaching(
        db,
        step_results=_steps(),
        report=_report(),
        external_allowed=False,
        provider=_CoachingProvider(),
    )

    assert result["source"] == "deterministic_rules"
    assert "工具选择错误" in result["explanation"]
    assert result["receipt_ids"] == []
    assert db.query(ProviderReceiptRow).count() == 0


def test_external_coaching_is_bounded_and_cannot_change_score(db, monkeypatch) -> None:
    monkeypatch.setenv("GONGXU_LLM_MODE", "live")
    from gongxu.config import get_settings

    get_settings.cache_clear()
    report = _report()
    result = generate_coaching(
        db,
        step_results=_steps(),
        report=report,
        external_allowed=True,
        provider=_CoachingProvider(),
    )

    assert result["source"] == "external_model"
    assert result["practice_plan"] == {
        "focus_steps": ["step-2"],
        "repeat_count": 4,
        "note": "完成四次无错操作后再考评。",
    }
    assert "total_score" not in result
    assert report["total_score"] == 38
    assert len(result["receipt_ids"]) == 2
    assert db.query(ProviderReceiptRow).count() == 2
