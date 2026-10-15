"""实验运行器：固定随机种子、确定性评估、结果归档（handoff §8/§15）。

- 固定种子保证可复现
- 结果写入 reports/generated/training-metrics.json
- 门槛是计划目标，不是现有成绩；未达到或未在真实数据上验证时标记 experimental
"""

from __future__ import annotations

import datetime
import json
import random
from pathlib import Path
from typing import Any

from gongxu.config import get_settings
from gongxu.ml.metrics import evaluate_sequences
from gongxu.sop.fixtures import grinder_disassembly_sop
from gongxu.vision import align, facts
from gongxu.vision.fixture import fixture_detections

TARGETS = {
    "step_macro_f1": 0.85,
    "miss_recall": 0.90,
    "miss_precision": 0.85,
}


def _reference_sequence() -> list[int]:
    return [s.index for s in grinder_disassembly_sop().steps]


def _predicted_sequence(
    detections: list[facts.Detection], scenario: str
) -> tuple[list[int], dict[str, Any]]:
    """用对齐器得到步骤序列，返回 (passed_indices, alignment_stats)。"""
    sop = grinder_disassembly_sop()
    observations = facts.build_observations(detections)
    alignment = align.align_observations(sop.steps, observations)
    passed = [r.step.index for r in alignment.step_results if r.judgement == "passed"]
    return passed, {"observations": len(observations)}


def run_fixture_experiment(*, seed: int = 42) -> dict[str, Any]:
    """在三种确定性场景上运行对齐+指标，归档真实结果。

    仅证明工程链路可用；不代表真实工厂准确率（标注 experimental_unvalidated）。
    """
    random.seed(seed)
    ref = _reference_sequence()
    per_scenario: list[dict[str, Any]] = []

    scenarios = ["pass", "missing_step_1", "wrong_tool_step_5"]
    for scenario in scenarios:
        detections = fixture_detections(total_keyframes=24, scenario=scenario)
        predicted, stats = _predicted_sequence(detections, scenario)
        metrics = evaluate_sequences(ref, predicted)
        per_scenario.append(
            {
                "scenario": scenario,
                "reference": ref,
                "predicted": predicted,
                **metrics,
                **stats,
            }
        )

    # 聚合（等权平均）
    agg: dict[str, float] = {}
    keys = [
        "step_macro_f1",
        "step_precision",
        "step_recall",
        "edit_distance",
        "miss_recall",
        "miss_precision",
        "wrong_order_recall",
        "wrong_order_precision",
    ]
    for key in keys:
        agg[key] = round(sum(s[key] for s in per_scenario) / len(per_scenario), 4)

    met_targets = all(agg[k] >= v for k, v in TARGETS.items())
    experiment = {
        "experiment_id": f"fixture-align-{datetime.datetime.now(datetime.UTC).date().isoformat()}",
        "mode": "fixture",
        "seed": seed,
        "framework": "deterministic-aligner (no learned params)",
        "validation": "experimental_unvalidated",
        "note": "fixture 场景仅验证工程链路；准确率需在真实 IMPACT/工装数据上重跑",
        "targets": TARGETS,
        "met_targets": met_targets,
        "aggregate": agg,
        "per_scenario": per_scenario,
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    return experiment


def save_experiment(experiment: dict[str, Any], target: Path | None = None) -> Path:
    dest = target or (
        get_settings().data_root.parent / "reports" / "generated" / "training-metrics.json"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(experiment, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


if __name__ == "__main__":
    exp = run_fixture_experiment()
    path = save_experiment(exp)
    print(f"experiment archived: {path}")
    print(f"  aggregate: {exp['aggregate']}")
    print(f"  met_targets: {exp['met_targets']}  ({exp['validation']})")
