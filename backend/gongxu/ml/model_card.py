"""模型卡自动生成：从归档实验输出 docs/MODEL_CARD.md。

明确区分：计划门槛 vs 归档实验结果 vs 真实部署验证。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast


def render_model_card(experiment: dict[str, Any]) -> str:
    agg = experiment.get("aggregate", {})
    lines = [
        f"# 模型卡：{experiment.get('experiment_id', 'unknown')}",
        "",
        f"- 实验模式：{experiment.get('mode', 'fixture')}",
        f"- 框架：{experiment.get('framework', '')}",
        f"- 随机种子：{experiment.get('seed', '')}",
        f"- 验证状态：**{experiment.get('validation', 'experimental_unvalidated')}**",
        f"- 说明：{experiment.get('note', '')}",
        "",
        "## 计划门槛（§9，非现有成绩）",
        "",
        "| 指标 | 门槛 | 归档结果 | 达标 |",
        "|---|---|---|---|",
    ]
    targets = experiment.get("targets", {})
    for key, threshold in targets.items():
        actual = agg.get(key, 0.0)
        lines.append(
            f"| {key} | {threshold} | {actual} | {'✅' if actual >= threshold else '❌'} |"
        )
    lines += [
        "",
        "## 归档实验逐场景结果",
        "",
        "| 场景 | 步骤宏F1 | 编辑距离 | 漏项召回 | 错序精确率 | 观察数 |",
        "|---|---|---|---|---|---|",
    ]
    for row in experiment.get("per_scenario", []):
        lines.append(
            f"| {row['scenario']} | {row['step_macro_f1']} | {row['edit_distance']} "
            f"| {row['miss_recall']} | {row['wrong_order_precision']} | {row['observations']} |"
        )
    lines += [
        "",
        "## 诚实边界",
        "",
        "- fixture 场景由确定性视觉源生成，只验证工程链路（视频→观察→对齐→评分）。",
        "- 真实准确率必须由归档实验在 IMPACT 或目标工装数据上重跑后更新本卡。",
        "- 未达到门槛或未在真实数据上验证时，模型标记 `experimental`，不得篡改阈值。",
    ]
    return "\n".join(lines) + "\n"


def write_model_card(experiment: dict[str, Any], target: Path | None = None) -> Path:
    from gongxu.config import get_settings

    dest = target or (get_settings().data_root.parent / "docs" / "MODEL_CARD.md")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_model_card(experiment), encoding="utf-8")
    return dest


def load_experiment(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
