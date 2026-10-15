"""Build offline contest replays through the production alignment and scoring code.

These scenarios prove product behaviour when a network Provider is unavailable. They are
engineering fixtures, not model-accuracy measurements, and every API response says so.
"""

from __future__ import annotations

from typing import Any, Literal

from gongxu.assessment.service import SCORING_RULES_VERSION
from gongxu.scoring.engine import score_assessment
from gongxu.sop.fixtures import grinder_disassembly_sop
from gongxu.vision.align import align_observations
from gongxu.vision.facts import Observation, build_observations, visible_frame_ratio
from gongxu.vision.fixture import fixture_detections

DemoScenario = Literal["complete", "error", "inconclusive"]

_FIXTURE_SCENARIOS: dict[DemoScenario, str] = {
    "complete": "pass",
    "error": "wrong_tool_step_5",
    "inconclusive": "missing_step_1",
}

_LABELS: dict[DemoScenario, str] = {
    "complete": "完整操作",
    "error": "明确错误",
    "inconclusive": "视角与证据不足",
}


class DemoScenarioError(ValueError):
    """Raised when an unknown replay scenario is requested."""


def build_demo_scenario(scenario: str) -> dict[str, Any]:
    """Run one fixture event stream through the real aligner and scoring engine."""
    if scenario not in _FIXTURE_SCENARIOS:
        raise DemoScenarioError(f"未知演示场景: {scenario}")
    typed_scenario = scenario
    fixture_name = _FIXTURE_SCENARIOS[typed_scenario]
    sop = grinder_disassembly_sop()
    detections = fixture_detections(total_keyframes=24, scenario=fixture_name)
    observations = build_observations(detections)
    alignment = align_observations(sop.steps, observations)
    scoring = score_assessment(sop.steps, alignment.step_results, sop.scoring_profile)

    steps = []
    for result in alignment.step_results:
        evidence = _evidence_for_result(observations, result.start_ms, result.end_ms)
        steps.append(
            {
                "step_index": result.step.index,
                "step_id": result.step.id,
                "step_name": result.step.name,
                "judgement": result.judgement,
                "start_ms": result.start_ms,
                "end_ms": result.end_ms,
                "rule_code": result.rule_code,
                "reason": result.reason,
                "confidence": result.confidence,
                "keyframe_ref": None,
                "detected_tools": sorted({item.tool for item in evidence if item.tool}),
                "detected_objects": sorted({item.object for item in evidence if item.object}),
                "detected_actions": sorted({item.action for item in evidence if item.action}),
                "observable_check_results": dict(result.observable_check_results),
            }
        )

    return {
        "scenario": typed_scenario,
        "label": _LABELS[typed_scenario],
        "source": "fixture_replay",
        "cached": True,
        "disclaimer": (
            "离线工程回放：事件由确定性 fixture 产生，并经过正式对齐与评分代码；"
            "用于演示产品行为，不代表真实模型准确率或工业认证结果。"
        ),
        "sop": {
            "id": sop.id,
            "process_key": sop.process_key,
            "version": sop.version,
        },
        "report": {
            "id": f"demo-report-{typed_scenario}",
            "session_id": f"demo-session-{typed_scenario}",
            "total_score": scoring.total_score,
            "result": scoring.result,
            "dimension_scores": scoring.dimension_scores,
            "evidence_coverage": scoring.evidence_coverage,
            "model_version": "fixture-replay-v1",
            "scoring_rules_version": SCORING_RULES_VERSION,
        },
        "steps": steps,
        "observations": [
            {
                "timestamp_ms": item.timestamp_ms,
                "hand": item.hand,
                "tool": item.tool,
                "object": item.object,
                "action": item.action,
                "relation": item.relation,
                "confidence": item.confidence,
            }
            for item in observations
        ],
        "quality": {
            "sampled_frames": 24,
            "visible_ratio": visible_frame_ratio(detections, 24),
        },
        "preview_url": None,
    }


def _evidence_for_result(
    observations: list[Observation], start_ms: int, end_ms: int
) -> list[Observation]:
    """Return observations inside a result interval, including point evidence."""
    if start_ms == 0 and end_ms == 0:
        return []
    high = max(start_ms, end_ms)
    return [item for item in observations if start_ms <= item.timestamp_ms <= high]
