"""确定性评分引擎（handoff §10）。

分数由固定规则计算，大模型不得修改数值。评分权重随 SOP 版本归档。

规则：
- 步骤完整性 30% / 操作顺序 20% / 工具与零件 15% / 动作规范 20% /
  操作效率 5% / 可观察安全规范 10%。
- 关键步骤缺失 → 总分最高 59。
- 不可观察权重 > 20% → result=inconclusive，只显示临时分数与证据覆盖率。
- 完成状态的缺失证据作为复核提示，不单独否决已被充分识别的操作。
- 其余情况只对可观察项目归一化，不把看不见当作错误。
- 每个扣分项绑定规则代码与时间区间（由调用方从 step_assessments 提供）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gongxu.sop.schema import ScoringProfile, SopStep

MIN_EVIDENCE_COVERAGE = 0.80
MAX_UNOBSERVABLE_RATIO = 0.20
PASS_SCORE = 60.0
CRITICAL_FAILURE_CAP = 59.0
MIN_EFFICIENCY_EVIDENCE_MS = 1000


class StepOutcome:
    passed = "passed"
    failed = "failed"
    unobservable = "unobservable"
    insufficient_evidence = "insufficient_evidence"


@dataclass
class StepResult:
    """步骤判定输入。"""

    step: SopStep
    judgement: str
    start_ms: int = 0
    end_ms: int = 0
    rule_code: str = ""
    reason: str = ""
    confidence: float = 0.0
    keyframe_ref: str | None = None
    # Exact SOP check text -> passed | failed | insufficient_evidence.
    # Missing/empty data is unknown; a passed step never implies these checks.
    observable_check_results: dict[str, str] = field(default_factory=dict)


@dataclass
class Deduction:
    rule_code: str
    step_index: int
    reason: str
    start_ms: int = 0
    end_ms: int = 0


@dataclass
class ScoreResult:
    total_score: float
    result: str  # pass | fail | inconclusive
    dimension_scores: dict[str, float] = field(default_factory=dict)
    evidence_coverage: float = 0.0
    deductions: list[Deduction] = field(default_factory=list)
    critical_step_missing: bool = False
    unobservable_weight: float = 0.0


def _dimension_score(step_results: list[StepResult], total_weight: float) -> float:
    """passed 步权重和 / 总权重（对可观察项目归一化）。"""
    if total_weight <= 0:
        return 0.0
    passed_weight = sum(r.step.weight for r in step_results if r.judgement == StepOutcome.passed)
    return round(passed_weight / total_weight * 100, 2)


def score_assessment(
    steps: list[SopStep],
    step_results: list[StepResult],
    profile: ScoringProfile | None = None,
) -> ScoreResult:
    """按 SOP 步骤判定计算确定性分数。"""
    profile = profile or ScoringProfile()
    result_by_index = {r.step.index: r for r in step_results}

    # 未提供的步骤按 insufficient_evidence 处理
    resolved: list[StepResult] = []
    for step in steps:
        if step.index in result_by_index:
            resolved.append(result_by_index[step.index])
        else:
            resolved.append(
                StepResult(
                    step=step,
                    judgement=StepOutcome.insufficient_evidence,
                    rule_code="missing_step_evidence",
                    reason="未提供该步骤的判定",
                )
            )

    # optional 的明确语义：可选步骤不属于必做评分项。其缺失/不可观察不进入覆盖率、
    # 完整性或关键拒判；即使被观察到，也不会给必做任务额外加分。
    scored_steps = [step for step in steps if not step.optional]
    scored_indices = {step.index for step in scored_steps}
    scored_results = [result for result in resolved if result.step.index in scored_indices]

    # 证据覆盖率 = 必做步骤中非 insufficient_evidence 的步骤权重占比
    total_weight = sum(s.weight for s in scored_steps)
    covered_weight = sum(
        r.step.weight
        for r in scored_results
        if r.judgement != StepOutcome.insufficient_evidence
    )
    evidence_coverage = round(covered_weight / total_weight, 4) if total_weight else 0.0

    # 不可观察权重占比（只对明确 unobservable 计数）
    unobservable_weight = sum(
        r.step.weight
        for r in scored_results
        if r.judgement == StepOutcome.unobservable
    )
    unobservable_ratio = unobservable_weight / total_weight if total_weight else 0.0

    # 可观察项目（passed/failed）
    observable = [
        r
        for r in scored_results
        if r.judgement in (StepOutcome.passed, StepOutcome.failed)
    ]

    # 维度分数：分母用全步骤权重（而非仅可观察权重），让 insufficient_evidence 的步骤
    # 按"未完成"计入——否则只归一化可观察项目会让 completeness 虚高（例如 6 步只
    # 识别到 2 步且都 passed 时，旧逻辑算 100/100=100，实际只完成 40%）。
    completeness = _dimension_score(observable, total_weight)  # 完整性
    order = _order_score(scored_steps, scored_results)
    tools_parts = _tools_parts_score(scored_results, total_weight)
    action_quality = _action_quality_score(scored_results, total_weight)
    efficiency = _efficiency_score(scored_results)
    safety = _safety_score(scored_results, total_weight)

    total = (
        completeness * profile.completeness
        + order * profile.order
        + tools_parts * profile.tools_parts
        + action_quality * profile.action_quality
        + efficiency * profile.efficiency
        + safety * profile.observable_safety
    )
    total = round(total, 2)

    deductions = _collect_deductions(resolved)

    # 关键步骤明确失败 → 封顶 59。
    # 注意：insufficient_evidence（证据不足/未识别到）不触发封顶——"没看见"不等同于
    # "操作者做错"。qwen 等视觉识别对工业视频漏检率高，若把漏检当失败封顶，会导致
    # 只要任一关键步未被识别到就把分数锁死在 59，无法反映真实识别到的部分。
    # 漏检的影响通过 evidence_coverage（证据覆盖率）体现，而非封顶。
    critical_missing = any(
        r.step.critical and r.judgement == StepOutcome.failed for r in scored_results
    )
    if critical_missing:
        total = min(total, CRITICAL_FAILURE_CAP)

    critical_insufficient = any(
        r.step.critical
        and r.judgement in (StepOutcome.insufficient_evidence, StepOutcome.unobservable)
        for r in scored_results
    )
    critical_safety_insufficient = any(
        r.step.critical
        and r.judgement != StepOutcome.failed
        and _has_incomplete_observable_checks(r)
        for r in scored_results
    )
    # 完成状态是动作后的静态画面证据。第一视角或遮挡视频常看不清最终卡位，
    # 但不应覆盖掉已经充分识别的完整操作。它仍作为步骤提示和扣分依据保留，
    # 不能独自把高覆盖率、高分的操作降级为“无法判断”。

    # 证据不足时 total_score 仅是临时分：关键步骤没看清、关键可观察安全
    # 规范未确认，或总覆盖率低于 80%，都不允许对外呈现为通过/失败。
    if critical_missing:
        # An explicit critical violation is already sufficient for a fail;
        # unrelated unknown checks must not weaken that finding to inconclusive.
        result = "fail"
    elif (
        critical_insufficient
        or critical_safety_insufficient
        or evidence_coverage < MIN_EVIDENCE_COVERAGE
        or unobservable_ratio > MAX_UNOBSERVABLE_RATIO
    ):
        result = "inconclusive"
    else:
        result = "pass" if total >= PASS_SCORE else "fail"

    return ScoreResult(
        total_score=total,
        result=result,
        dimension_scores={
            "completeness": round(completeness, 2),
            "order": round(order, 2),
            "tools_parts": round(tools_parts, 2),
            "action_quality": round(action_quality, 2),
            "efficiency": round(efficiency, 2),
            "observable_safety": round(safety, 2),
        },
        evidence_coverage=evidence_coverage,
        deductions=deductions,
        critical_step_missing=critical_missing,
        unobservable_weight=round(unobservable_weight, 2),
    )


def _order_score(steps: list[SopStep], results: list[StepResult]) -> float:
    """前置依赖完成情况：passed 步骤中前置是否已通过。

    仅当前置是 failed（明确没做/做错）才计违规；前置是 insufficient_evidence
    （没识别到，不确定）时跳过——"没看见"不等同于"顺序错"，不应扣分。
    """
    passed_indices = {r.step.index for r in results if r.judgement == StepOutcome.passed}
    if not passed_indices:
        return 0.0
    failed_indices = {r.step.index for r in results if r.judgement == StepOutcome.failed}
    violations = 0
    total_checked = 0
    for r in results:
        if r.judgement != StepOutcome.passed:
            continue
        for pred in r.step.predecessors:
            # 前置 failed → 确实顺序违规；前置 insufficient/unobservable → 不确定，跳过
            if pred in failed_indices:
                total_checked += 1
                violations += 1
            elif pred in passed_indices:
                total_checked += 1
            # 前置 insufficient_evidence/unobservable：无法判断，不计入检查
    if total_checked == 0:
        return 100.0  # 没有可检查的顺序约束，不扣分
    return round(max(0.0, 1 - violations / total_checked) * 100, 2)


def _tools_parts_score(results: list[StepResult], _total_weight: float) -> float:
    """工具/零件维度只在声明了工具或对象的适用步骤内归一化。"""
    checked = [r for r in results if r.step.tools or r.step.objects]
    if not checked:
        return 100.0
    applicable_weight = sum(result.step.weight for result in checked)
    if applicable_weight <= 0:
        return 0.0
    ok_weight = sum(r.step.weight for r in checked if r.judgement == StepOutcome.passed)
    return round(ok_weight / applicable_weight * 100, 2)


def _action_quality_score(results: list[StepResult], total_weight: float) -> float:
    """动作规范：passed 步骤权重占比（分母=全步骤权重）。"""
    if total_weight <= 0:
        return 0.0
    checked = [r for r in results if r.judgement in (StepOutcome.passed, StepOutcome.failed)]
    if not checked:
        return 0.0
    ok_weight = sum(r.step.weight for r in checked if r.judgement == StepOutcome.passed)
    return round(ok_weight / total_weight * 100, 2)


def _efficiency_score(results: list[StepResult]) -> float:
    """效率：仅对具有至少 1 秒可信连续区间的通过步骤计算。

    单帧/时间点证据能够证明瞬时关系，但不能证明真实操作用时；将其按 1ms
    计算会产生虚假的 100 分效率。
    """
    checked = [
        r
        for r in results
        if r.judgement == StepOutcome.passed
        and r.step.expected_duration_s > 0
        and r.end_ms - r.start_ms >= MIN_EFFICIENCY_EVIDENCE_MS
    ]
    if not checked:
        return 0.0
    ratios = []
    for r in checked:
        expected_ms = r.step.expected_duration_s * 1000
        actual_ms = max(0, r.end_ms - r.start_ms)
        ratios.append(min(1.0, expected_ms / max(actual_ms, 1)))
    return round(sum(ratios) / len(ratios) * 100, 2)


def _safety_score(results: list[StepResult], _total_weight: float) -> float:
    """Score only explicit per-check visual facts, never the whole-step outcome.

    A tool/object/action match can prove that an operation happened, but it does
    not prove PPE, isolation or any other ``observable_checks`` requirement.
    Each check receives an equal share of its step weight; unknown checks earn
    no provisional safety credit and critical unknowns force inconclusive.
    """
    checked = [r for r in results if r.step.observable_checks]
    if not checked:
        return 100.0
    applicable_weight = sum(result.step.weight for result in checked)
    if applicable_weight <= 0:
        return 0.0
    ok_weight = 0.0
    for result in checked:
        statuses = _observable_check_statuses(result)
        per_check_weight = result.step.weight / len(result.step.observable_checks)
        ok_weight += sum(
            per_check_weight
            for check in result.step.observable_checks
            if statuses.get(check) == StepOutcome.passed
        )
    return round(ok_weight / applicable_weight * 100, 2)


def _observable_check_statuses(result: StepResult) -> dict[str, str]:
    """Return controlled check states, with a narrow persisted-code fallback.

    Exact per-check facts are authoritative. The rule-code fallback exists only
    for legacy rows that predate per-check persistence; arbitrary ``passed`` or
    ``ok`` values never become safety evidence.
    """
    allowed = {
        StepOutcome.passed,
        StepOutcome.failed,
        StepOutcome.insufficient_evidence,
    }
    statuses = {
        check: status
        for check, status in result.observable_check_results.items()
        if check in result.step.observable_checks and status in allowed
    }
    if statuses:
        return {
            check: statuses.get(check, StepOutcome.insufficient_evidence)
            for check in result.step.observable_checks
        }
    if result.rule_code == "observed_checks_passed":
        return {check: StepOutcome.passed for check in result.step.observable_checks}
    if result.rule_code == "observable_check_failed":
        return {check: StepOutcome.failed for check in result.step.observable_checks}
    if result.rule_code == "observable_checks_incomplete":
        return {
            check: StepOutcome.insufficient_evidence for check in result.step.observable_checks
        }
    return {
        check: StepOutcome.insufficient_evidence for check in result.step.observable_checks
    }


def _has_incomplete_observable_checks(result: StepResult) -> bool:
    statuses = _observable_check_statuses(result)
    return bool(statuses) and any(
        status == StepOutcome.insufficient_evidence for status in statuses.values()
    )


def _completion_check_statuses(result: StepResult) -> dict[str, str]:
    """Return exact completion-state evidence without treating it as safety evidence."""
    allowed = {
        StepOutcome.passed,
        StepOutcome.failed,
        StepOutcome.insufficient_evidence,
    }
    return {
        check: status
        for check, status in result.observable_check_results.items()
        if check in result.step.completion_checks and status in allowed
    }


def _has_incomplete_completion_checks(result: StepResult) -> bool:
    if not result.step.completion_checks:
        return False
    statuses = _completion_check_statuses(result)
    return any(
        statuses.get(check, StepOutcome.insufficient_evidence)
        == StepOutcome.insufficient_evidence
        for check in result.step.completion_checks
    )


def _collect_deductions(results: list[StepResult]) -> list[Deduction]:
    deductions = []
    for r in results:
        if r.judgement == StepOutcome.failed:
            deductions.append(
                Deduction(
                    rule_code=r.rule_code or "step_failed",
                    step_index=r.step.index,
                    reason=r.reason or f"步骤 {r.step.index} 未通过",
                    start_ms=r.start_ms,
                    end_ms=r.end_ms,
                )
            )
        elif r.judgement == StepOutcome.insufficient_evidence and not r.step.optional:
            deductions.append(
                Deduction(
                    rule_code=r.rule_code or "insufficient_evidence",
                    step_index=r.step.index,
                    reason=r.reason or f"步骤 {r.step.index} 证据不足，未判分",
                    start_ms=r.start_ms,
                    end_ms=r.end_ms,
                )
            )
        elif (
            r.judgement == StepOutcome.passed
            and not r.step.optional
            and _has_incomplete_observable_checks(r)
        ):
            deductions.append(
                Deduction(
                    rule_code="observable_safety_insufficient",
                    step_index=r.step.index,
                    reason=f"步骤 {r.step.index} 的可观察检查证据不完整，安全维度未计满分",
                    start_ms=r.start_ms,
                    end_ms=r.end_ms,
            )
        )
        elif (
            r.judgement == StepOutcome.passed
            and not r.step.optional
            and _has_incomplete_completion_checks(r)
        ):
            deductions.append(
                Deduction(
                    rule_code="completion_evidence_insufficient",
                    step_index=r.step.index,
                    reason=(
                        f"步骤 {r.step.index} 的完成状态未获得足够视觉证据，"
                        "不将动作发生误报为完成正确"
                    ),
                    start_ms=r.start_ms,
                    end_ms=r.end_ms,
                )
            )
    return deductions
