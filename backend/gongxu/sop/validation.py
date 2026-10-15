"""SOP 业务校验：非法大模型输出无法入库（handoff §8）。

校验项：
- JSON Schema 已由 Pydantic 保证；
- 步骤编号连续（1..N）；
- 前置依赖构成无环图；
- 前置 index 必须存在；
- 权重总和 = 100（容忍浮点误差）；
- 每个可观察检查必须有证据引用；
- 来源等级与证据 kind 兼容，并且等级声明必须由相应证据支撑。
"""

from __future__ import annotations

from typing import Literal

from gongxu.sop.schema import EvidenceRef, SopStep, SopVersion, SourceGrade


class SopValidationError(Exception):
    pass


def _check_dag_acyclic(steps: list[SopStep]) -> None:
    """前置依赖必须无环：拓扑排序检测。"""
    in_degree: dict[int, int] = {s.index: 0 for s in steps}
    adjacency: dict[int, list[int]] = {s.index: [] for s in steps}
    for s in steps:
        for pred in s.predecessors:
            if pred not in in_degree:
                raise SopValidationError(f"步骤 {s.index} 的前置 {pred} 不存在")
            adjacency[pred].append(s.index)
            in_degree[s.index] += 1

    queue = [i for i in in_degree if in_degree[i] == 0]
    visited = 0
    while queue:
        node = queue.pop(0)
        visited += 1
        for nxt in adjacency[node]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)
    if visited != len(in_degree):
        raise SopValidationError("步骤依赖图存在环")


def _evidence_kind_allowed(kind: str, grade: SourceGrade) -> bool:
    if grade == SourceGrade.sensor_verified:
        return kind in ("video", "manual", "sensor")
    if grade == SourceGrade.manual_backed:
        return kind in ("video", "manual")
    return kind == "video"


def _validate_step(step: SopStep, grade: SourceGrade) -> None:
    if not step.completion_checks and not step.observable_checks and not step.unobservable_checks:
        raise SopValidationError(
            f"步骤 {step.index} 缺少通过条件（completion/observable/unobservable checks）"
        )
    if (step.completion_checks or step.observable_checks) and not (
        step.tools or step.objects or step.actions
    ):
        raise SopValidationError(
            f"步骤 {step.index} 包含视觉检查，但缺少工具、对象或动作语义，"
            "无法与视频事件可靠对齐"
        )
    if not step.evidence_refs:
        raise SopValidationError(f"步骤 {step.index} 缺少来源证据")
    for ref in step.evidence_refs:
        if not ref.ref.strip():
            raise SopValidationError(f"步骤 {step.index} 的来源证据引用为空")
        if not _evidence_kind_allowed(ref.kind, grade):
            raise SopValidationError(
                f"步骤 {step.index} 的证据 {ref.kind} 与来源等级 {grade.value} 不兼容"
            )
    if grade == SourceGrade.video_reference and not any(
        ref.kind == "video" for ref in step.evidence_refs
    ):
        raise SopValidationError(f"video_reference 步骤 {step.index} 必须包含 video 来源证据")
    if grade == SourceGrade.manual_backed and not any(
        ref.kind in ("video", "manual") for ref in step.evidence_refs
    ):
        raise SopValidationError(
            f"manual_backed 步骤 {step.index} 必须包含 video 或 manual 可追溯证据"
        )


def _validate_grade_evidence(steps: list[SopStep], grade: SourceGrade) -> None:
    evidence_kinds = {ref.kind for step in steps for ref in step.evidence_refs}
    if grade == SourceGrade.manual_backed and "manual" not in evidence_kinds:
        raise SopValidationError("manual_backed SOP 至少需要一条 manual 来源证据")
    if grade == SourceGrade.sensor_verified and "sensor" not in evidence_kinds:
        raise SopValidationError("sensor_verified SOP 至少需要一条 sensor 来源证据")


def validate_sop(sop: SopVersion) -> SopVersion:
    """校验 SOP 并返回（不修改）。非法输出抛 SopValidationError。"""
    steps = sop.steps
    if not steps:
        raise SopValidationError("SOP 至少需要一个步骤")

    expected = list(range(1, len(steps) + 1))
    actual = sorted(s.index for s in steps)
    if actual != expected:
        raise SopValidationError(f"步骤编号必须连续 1..{len(steps)}，实际 {actual}")

    _check_dag_acyclic(steps)

    weight_sum = sum(s.weight for s in steps)
    if abs(weight_sum - 100.0) > 0.01:
        raise SopValidationError(f"步骤权重总和必须为 100，实际 {weight_sum:.2f}")

    for step in steps:
        _validate_step(step, sop.source_grade)
    _validate_grade_evidence(steps, sop.source_grade)

    profile = sop.scoring_profile
    profile_sum = (
        profile.completeness
        + profile.order
        + profile.tools_parts
        + profile.action_quality
        + profile.efficiency
        + profile.observable_safety
    )
    if abs(profile_sum - 1.0) > 1e-6:
        raise SopValidationError(f"评分维度权重总和必须为 1，实际 {profile_sum}")

    return sop


def make_evidence(
    video_ref: str,
    *,
    kind: Literal["video", "manual", "sensor"] = "video",
    description: str = "",
) -> EvidenceRef:
    return EvidenceRef(kind=kind, ref=video_ref, description=description)
