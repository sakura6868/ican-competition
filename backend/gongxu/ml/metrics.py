"""评估指标（handoff §9/§15）：步骤宏 F1、编辑距离、漏项/错序 PR。

所有指标在 `ml/experiment` 中由归档实验产生；本模块只做纯函数计算。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class EvalResult:
    step_macro_f1: float
    step_precision: float
    step_recall: float
    edit_distance: int
    miss_recall: float
    miss_precision: float
    wrong_order_recall: float
    wrong_order_precision: float
    sample_count: int


def _f1(p: float, r: float) -> float:
    if p + r == 0:
        return 0.0
    return 2 * p * r / (p + r)


def edit_distance(a: list[int], b: list[int]) -> int:
    """两个步骤序列的 Levenshtein 距离（漏项/错序/重复的近似度量）。"""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(
                min(
                    prev[j] + 1,  # 删除
                    cur[j - 1] + 1,  # 插入
                    prev[j - 1] + (0 if x == y else 1),  # 替换
                )
            )
        prev = cur
    return prev[-1]


def evaluate_sequences(
    reference: list[int],
    predicted: list[int],
    *,
    critical_steps: set[int] | None = None,
) -> dict[str, Any]:
    """按步骤序列对比参考与预测，输出漏项/错序指标。

    - miss：参考步骤在预测中缺失（覆盖不足）
    - wrong_order：相对顺序错误（最小编辑中的替换/换位）
    """
    ref_set = set(reference)
    pred_set = set(predicted)

    # 漏项：参考中缺失的步骤。miss_recall = 未漏掉的参考步骤占比（越高越好）
    miss_actual = ref_set - pred_set
    miss_recall = (len(ref_set) - len(miss_actual)) / len(ref_set) if ref_set else 1.0
    # miss_precision = 预测步骤中确实属于参考的比例（无额外步骤时 =1）
    miss_precision = len(ref_set & pred_set) / len(pred_set) if pred_set else 0.0

    # 错序：预测中公共步骤的相对顺序与参考不一致的次数
    common = ref_set & pred_set
    ref_order = {s: i for i, s in enumerate(reference) if s in common}
    pred_common = [s for s in predicted if s in common]
    wrong_order_count = 0
    for i in range(len(pred_common)):
        for j in range(i + 1, len(pred_common)):
            if ref_order[pred_common[i]] > ref_order[pred_common[j]]:
                wrong_order_count += 1
    max_pairs = len(common) * (len(common) - 1) // 2 if len(common) >= 2 else 0
    order_correct_ratio = 1 - wrong_order_count / max_pairs if max_pairs else 1.0
    wrong_order_recall = order_correct_ratio
    wrong_order_precision = order_correct_ratio

    # 步骤宏 F1：按每个步骤的命中/漏报/误报计算
    step_f1s: list[float] = []
    for step in sorted(ref_set | pred_set):
        tp = 1 if (step in ref_set and step in pred_set) else 0
        fp = 1 if (step in pred_set and step not in ref_set) else 0
        fn = 1 if (step in ref_set and step not in pred_set) else 0
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        step_f1s.append(_f1(precision, recall))

    macro_f1 = sum(step_f1s) / len(step_f1s) if step_f1s else 0.0
    step_precision = sum(1 for s in ref_set & pred_set) / len(pred_set) if pred_set else 0.0
    step_recall = sum(1 for s in ref_set & pred_set) / len(ref_set) if ref_set else 0.0

    return {
        "step_macro_f1": round(macro_f1, 4),
        "step_precision": round(step_precision, 4),
        "step_recall": round(step_recall, 4),
        "edit_distance": edit_distance(reference, predicted),
        "miss_recall": round(miss_recall, 4),
        "miss_precision": round(miss_precision, 4),
        "wrong_order_precision": round(wrong_order_precision, 4),
        "wrong_order_recall": round(wrong_order_recall, 4),
        "sample_count": 1,
    }
