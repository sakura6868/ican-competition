"""确定性步骤对齐：观察事件流 → SOP 步骤判定（handoff §6/§9/§10）。

- 步骤通过：观察满足该步骤声明的每个工具/对象/动作类别要求；类别内取值为备选项。
- 错工具/错对象：观察到对象/动作但工具不符 → failed（wrong_tool / wrong_object）。
- 无证据：insufficient_evidence（不伪判、不直接扣分）。
- 仅不可观察检查的步骤 → unobservable（视频无法验证）。
- 对齐结果仍由评分引擎计算分数；本模块只产生判定。

判定顺序（确定性）：按步骤 index 升序逐条匹配，每个观察只归属最早匹配的步骤。
"""

from __future__ import annotations

from dataclasses import dataclass

from gongxu.scoring.engine import StepResult
from gongxu.sop.schema import SopStep
from gongxu.vision.facts import (
    OBSERVABLE_CHECK_FAILED,
    OBSERVABLE_CHECK_INSUFFICIENT,
    OBSERVABLE_CHECK_PASSED,
    RELATION_CONFIDENCE_THRESHOLD,
    Observation,
)


@dataclass
class AlignmentResult:
    step_results: list[StepResult]
    matched_observation_count: int
    reject_reason: str = ""


_MIN_MATCH_SCORE = 1.0


def _match_score(step: SopStep, obs: Observation) -> float:
    """计算观察与步骤的语义覆盖；所有已声明类别都必须有匹配证据。

    ``candidate_step_index`` 只用于将观察路由到候选步骤，既不加分，也不补足
    缺失的工具、对象或动作。SOP 中同一类别的多个值表示可接受备选项。
    """
    if obs.confidence < RELATION_CONFIDENCE_THRESHOLD:
        return 0.0
    if obs.candidate_step_index is not None and obs.candidate_step_index != step.index:
        return 0.0

    dimensions: list[tuple[str, list[str], float]] = [
        (obs.action, step.actions, 0.40),
        (obs.object, step.objects, 0.35),
        (obs.tool, step.tools, 0.25),
    ]
    required_weight = sum(weight for _value, allowed, weight in dimensions if allowed)
    if required_weight <= 0:
        return 0.0

    matched_weight = 0.0
    for value, allowed, weight in dimensions:
        if not allowed:
            continue
        if not value or value not in allowed:
            return 0.0
        matched_weight += weight

    score = matched_weight / required_weight
    return round(score, 4)


def _explicit_error(step: SopStep, obs: Observation) -> tuple[str, str] | None:
    """只在候选步骤或其余已声明语义类别明确匹配时判定错误。"""
    if obs.confidence < RELATION_CONFIDENCE_THRESHOLD:
        return None
    if obs.candidate_step_index is not None and obs.candidate_step_index != step.index:
        return None
    candidate_match = obs.candidate_step_index == step.index
    if (
        step.tools
        and obs.tool
        and obs.tool not in step.tools
        and (candidate_match or _other_dimensions_match(step, obs, excluded="tool"))
    ):
        return "wrong_tool", f"应使用 {step.tools}，观察到 {obs.tool}"
    if (
        step.objects
        and obs.object
        and obs.object not in step.objects
        and (candidate_match or _other_dimensions_match(step, obs, excluded="object"))
    ):
        return "wrong_object", f"应操作 {step.objects}，观察到 {obs.object}"
    if (
        step.actions
        and obs.action
        and obs.action not in step.actions
        and (candidate_match or _other_dimensions_match(step, obs, excluded="action"))
    ):
        return "wrong_action", f"应执行 {step.actions}，观察到 {obs.action}"
    return None


def _other_dimensions_match(
    step: SopStep, obs: Observation, *, excluded: str
) -> bool:
    """错误类别之外至少一个已声明类别必须完整匹配，避免无上下文误判。"""
    dimensions = {
        "tool": (obs.tool, step.tools),
        "object": (obs.object, step.objects),
        "action": (obs.action, step.actions),
    }
    matched_any = False
    for name, (value, allowed) in dimensions.items():
        if name == excluded or not allowed:
            continue
        if not value or value not in allowed:
            return False
        matched_any = True
    return matched_any


def _evidence_range(obs: Observation) -> tuple[int, int]:
    if obs.sample_count > 1:
        return obs.start_ms, max(obs.start_ms, obs.end_ms)
    return obs.timestamp_ms, obs.timestamp_ms


def _check_evidence_for_step(
    step: SopStep,
    observations: list[Observation],
    assignment: Observation | None,
) -> tuple[dict[str, str], Observation | None]:
    """Resolve declared visual facts without inferring them from action semantics.

    Completion and safety checks share the evidence transport, but are kept
    separate in the SOP and scoring layers.  The single exact-text map here is
    retained for backwards-compatible assessment persistence.
    """
    checks = _visual_checks(step)
    allowed = set(checks)
    statuses = {check: OBSERVABLE_CHECK_INSUFFICIENT for check in checks}
    strongest_failed: Observation | None = None

    for observation in observations:
        if observation.confidence < RELATION_CONFIDENCE_THRESHOLD:
            continue
        # A check may travel with the assigned semantic observation, or with a
        # check-only observation explicitly routed to this SOP step. Candidate
        # indices alone still do not prove a check.
        if observation is not assignment and observation.candidate_step_index != step.index:
            continue
        for check, status in observation.observable_check_results.items():
            if check not in allowed:
                continue
            if status == OBSERVABLE_CHECK_FAILED:
                statuses[check] = status
                if (
                    strongest_failed is None
                    or observation.confidence > strongest_failed.confidence
                ):
                    strongest_failed = observation
            elif (
                status == OBSERVABLE_CHECK_PASSED
                and statuses[check] == OBSERVABLE_CHECK_INSUFFICIENT
            ):
                statuses[check] = status
    return statuses, strongest_failed


def _check_summary(statuses: dict[str, str]) -> tuple[list[str], list[str]]:
    failed = [check for check, status in statuses.items() if status == OBSERVABLE_CHECK_FAILED]
    missing = [
        check for check, status in statuses.items() if status == OBSERVABLE_CHECK_INSUFFICIENT
    ]
    return failed, missing


def _visual_checks(step: SopStep) -> list[str]:
    """Return all checks that require visual evidence."""
    return [*step.completion_checks, *step.observable_checks]


def _failed_check_kind(step: SopStep, failed_checks: list[str]) -> str:
    return (
        "可观察安全检查"
        if any(check in step.observable_checks for check in failed_checks)
        else "完成状态检查"
    )


def _has_confirmed_visual_checks(result: StepResult) -> bool:
    """Order errors require final-state evidence for both compared steps.

    Reference-guided action anchors are intentionally sampled around an action,
    so their timestamps are not an operation clock.  A temporal inversion is
    a failure only once both operations have independently visible final-state
    checks; otherwise the system keeps the uncertainty as evidence-insufficient
    rather than turning sampling jitter into a wrong-order accusation.
    """
    checks = _visual_checks(result.step)
    return bool(checks) and all(
        result.observable_check_results.get(check) == OBSERVABLE_CHECK_PASSED
        for check in checks
    )


def align_observations(steps: list[SopStep], observations: list[Observation]) -> AlignmentResult:
    """将观察流对齐到步骤，返回确定性判定。

    判定规则：
    - 有匹配观察 → passed
    - 明确错工具/对象/动作 → failed
    - 已匹配步骤的时间违反前置关系 → failed (wrong_order)
    - 其余无证据 → insufficient_evidence：可能未识别到/尚未进行，不伪判

    先对所有“步骤-观察”边按匹配分数做全局贪心分配，避免通用动作
    （如 hold）被较早但较不具体的步骤抢走。
    """
    visual_steps = [step for step in steps if _visual_checks(step)]
    edges: list[tuple[float, float, int, int, SopStep, Observation]] = []
    earliest_match_ms: dict[int, int] = {}
    for step in visual_steps:
        for observation_index, observation in enumerate(observations):
            match_score = _match_score(step, observation)
            if match_score >= _MIN_MATCH_SCORE:
                match_start_ms = _evidence_range(observation)[0]
                earliest_match_ms[step.index] = min(
                    earliest_match_ms.get(step.index, match_start_ms),
                    match_start_ms,
                )
                edges.append(
                    (
                        match_score,
                        observation.confidence,
                        -observation.timestamp_ms,
                        observation_index,
                        step,
                        observation,
                    )
                )

    assigned_steps: set[int] = set()
    used: set[int] = set()
    assignments: dict[int, tuple[float, int, Observation]] = {}
    for score, _confidence, _negative_time, observation_index, step, observation in sorted(
        edges, key=lambda edge: (edge[0], edge[1], edge[2]), reverse=True
    ):
        if step.index in assigned_steps or observation_index in used:
            continue
        assigned_steps.add(step.index)
        used.add(observation_index)
        assignments[step.index] = (score, observation_index, observation)

    results: list[StepResult] = []
    matched = len(assignments)
    for step in steps:
        if not _visual_checks(step):
            # 仅不可观察检查：视频无法验证
            results.append(
                StepResult(
                    step=step,
                    judgement="unobservable",
                    rule_code="unobservable_only",
                    reason="该步骤仅含不可观察检查，视频无法验证",
                )
            )
            continue

        assignment = assignments.get(step.index)
        assigned_observation = assignment[2] if assignment is not None else None
        check_results, failed_check_observation = _check_evidence_for_step(
            step, observations, assigned_observation
        )
        failed_checks, missing_checks = _check_summary(check_results)
        if failed_checks:
            evidence_observation = failed_check_observation or assigned_observation
            start_ms, end_ms = (
                _evidence_range(evidence_observation)
                if evidence_observation is not None
                else (0, 0)
            )
            results.append(
                StepResult(
                    step=step,
                    judgement="failed",
                    start_ms=start_ms,
                    end_ms=end_ms,
                    rule_code=(
                        "observable_check_failed"
                        if any(check in step.observable_checks for check in failed_checks)
                        else "completion_check_failed"
                    ),
                    reason=(
                        f"步骤 {step.index} 明确违反"
                        f"{_failed_check_kind(step, failed_checks)}：{failed_checks}"
                    ),
                    confidence=(
                        evidence_observation.confidence
                        if evidence_observation is not None
                        else 0.0
                    ),
                    keyframe_ref=(
                        evidence_observation.frame_hash or None
                        if evidence_observation is not None
                        else None
                    ),
                    observable_check_results=check_results,
                )
            )
            continue

        if assignment is not None:
            score, _observation_index, obs = assignment
            start_ms, end_ms = _evidence_range(obs)
            context_note = (
                f"；视觉候选步骤 {obs.candidate_step_index}"
                if obs.candidate_step_index is not None
                else ""
            )
            evidence_note = (
                f"；可见依据：{obs.evidence_reason}"
                if obs.evidence_reason
                else ""
            )
            results.append(
                StepResult(
                    step=step,
                    judgement="passed",
                    start_ms=start_ms,
                    end_ms=end_ms,
                    rule_code=(
                        "completion_checks_incomplete"
                        if any(check in step.completion_checks for check in missing_checks)
                        else "observable_checks_incomplete"
                        if missing_checks
                        else "observed_checks_passed"
                    ),
                    reason=(
                        f"观察 {obs.relation_key} 命中步骤 {step.index}"
                        f"{context_note}{evidence_note}"
                        + (f"；未确认检查：{missing_checks}" if missing_checks else "")
                    ),
                    confidence=round(obs.confidence * max(score, 0.5), 4),
                    keyframe_ref=obs.frame_hash or None,
                    observable_check_results=check_results,
                )
            )
            continue

        wrong_candidates: list[tuple[int, Observation, str, str]] = []
        for observation_index, observation in enumerate(observations):
            if observation_index in used:
                continue
            explicit_error = _explicit_error(step, observation)
            if explicit_error is not None:
                code, reason = explicit_error
                wrong_candidates.append((observation_index, observation, code, reason))
        if wrong_candidates:
            idx, obs, code, error_reason = max(
                wrong_candidates, key=lambda item: item[1].confidence
            )
            used.add(idx)
            start_ms, end_ms = _evidence_range(obs)
            results.append(
                StepResult(
                    step=step,
                    judgement="failed",
                    start_ms=start_ms,
                    end_ms=end_ms,
                    rule_code=code,
                    reason=f"步骤 {step.index} {error_reason}",
                    confidence=obs.confidence,
                    keyframe_ref=obs.frame_hash or None,
                    observable_check_results={
                        check: OBSERVABLE_CHECK_INSUFFICIENT
                        for check in _visual_checks(step)
                    },
                )
            )
            continue

        results.append(
            StepResult(
                step=step,
                judgement="insufficient_evidence",
                rule_code="missing_evidence",
                reason=f"步骤 {step.index} 无匹配观察证据",
                observable_check_results={
                    check: OBSERVABLE_CHECK_INSUFFICIENT
                    for check in _visual_checks(step)
                },
            )
        )

    # 只用已匹配事件的真实时间确认错序。“后续步骤有证据”不能单独
    # 证明前置关键步被跳过，因为前置也可能只是视觉漏检。用于展示的
    # assignment 会偏向最高置信关键帧，因此顺序必须比较每步最早的有效
    # 匹配证据，不能拿较晚的高置信关键帧制造错序。
    by_index = {result.step.index: result for result in results}
    for result in results:
        if result.judgement != "passed":
            continue
        for predecessor_index in result.step.predecessors:
            predecessor = by_index.get(predecessor_index)
            if (
                predecessor is not None
                and predecessor.judgement == "passed"
                and _has_confirmed_visual_checks(predecessor)
                and _has_confirmed_visual_checks(result)
                and earliest_match_ms.get(predecessor_index, predecessor.start_ms)
                > earliest_match_ms.get(result.step.index, result.start_ms) + 2_500
            ):
                result.judgement = "failed"
                result.rule_code = "wrong_order"
                result.observable_check_results = {
                    check: OBSERVABLE_CHECK_INSUFFICIENT
                    for check in _visual_checks(result.step)
                }
                result.reason = (
                    f"步骤 {result.step.index} 的证据早于前置步骤 "
                    f"{predecessor_index}"
                )
                break

    return AlignmentResult(step_results=results, matched_observation_count=matched)
