"""确定性评分引擎测试（handoff §10 规则）。"""

from __future__ import annotations

from gongxu.scoring.engine import StepResult, score_assessment
from gongxu.sop.fixtures import grinder_disassembly_sop
from gongxu.sop.schema import SopStep


def _results_for(*indices: int) -> list[StepResult]:
    """给定通过步骤，其余一律按证据不足处理。"""
    sop = grinder_disassembly_sop()
    results = []
    for step in sop.steps:
        if step.index in indices:
            results.append(
                StepResult(
                    step=step,
                    judgement="passed",
                    start_ms=1000,
                    end_ms=step.expected_duration_s * 1000 + 1000,
                    rule_code="ok",
                    observable_check_results={
                        check: "passed" for check in step.observable_checks
                    },
                )
            )
        else:
            results.append(
                StepResult(
                    step=step,
                    judgement="insufficient_evidence",
                    rule_code="missing_evidence",
                    reason="未检测到",
                )
            )
    return results


def _all_passed() -> list[StepResult]:
    sop = grinder_disassembly_sop()
    return [
        StepResult(
            step=step,
            judgement="passed",
            start_ms=1000,
            end_ms=1000 + step.expected_duration_s * 1000,
            rule_code="ok",
            observable_check_results={check: "passed" for check in step.observable_checks},
        )
        for step in sop.steps
    ]


def test_all_passed_full_score() -> None:
    sop = grinder_disassembly_sop()
    result = score_assessment(sop.steps, _all_passed())
    assert result.result == "pass"
    assert result.total_score >= 95.0
    assert result.evidence_coverage == 1.0
    assert result.deductions == []


def test_critical_step_failed_caps_at_59() -> None:
    """关键步骤明确失败（错工具/做错）→ 总分最高 59 且 fail。"""
    sop = grinder_disassembly_sop()
    results = _results_for(2, 3, 4, 5, 6, 7)  # 步骤 1（critical）未通过
    # 把步骤 1 标为明确 failed（而非 insufficient_evidence）
    for r in results:
        if r.step.index == 1:
            r.judgement = "failed"
            r.rule_code = "wrong_tool"
            break
    result = score_assessment(sop.steps, results)
    assert result.critical_step_missing is True
    assert result.result == "fail"
    assert result.total_score <= 59.0


def test_critical_step_insufficient_does_not_cap() -> None:
    """关键步骤证据不足 → 不封顶，但最终必须拒判。

    修复点：insufficient_evidence（"没看见"）不等于 failed（"做错了"）。
    qwen 漏检率高，若把漏检当失败封顶，会把分数锁死 59 无法反映真实识别部分。
    漏检的影响通过 evidence_coverage 体现。
    """
    sop = grinder_disassembly_sop()
    passed = [i for i in range(1, 8) if i != 1]  # 步骤 1（critical）证据不足
    result = score_assessment(sop.steps, _results_for(*passed))
    assert result.critical_step_missing is False
    # 不封顶，分数能反映真实识别到的部分（>59 因为其余步骤都 passed 且无 failed）
    assert result.total_score > 59.0
    assert result.result == "inconclusive"


def test_missing_noncritical_does_not_cap() -> None:
    """非关键步骤缺失不触发 59 封顶。"""
    sop = grinder_disassembly_sop()
    result = score_assessment(sop.steps, _results_for(1, 2, 3, 4, 5, 6))
    assert result.critical_step_missing is False
    assert result.total_score > 59.0
    assert result.result == "pass"


def test_evidence_coverage_79_percent_is_inconclusive() -> None:
    """覆盖率 79% 低于门槛，总分只能作为临时分。"""
    sop = grinder_disassembly_sop()
    steps = [
        sop.steps[0].model_copy(update={"critical": False, "weight": 79.0}),
        sop.steps[1].model_copy(update={"critical": False, "weight": 21.0}),
    ]
    result = score_assessment(
        steps,
        [
            StepResult(
                step=steps[0],
                judgement="passed",
                rule_code="observed",
                observable_check_results={
                    check: "passed" for check in steps[0].observable_checks
                },
            )
        ],
    )
    assert result.evidence_coverage == 0.79
    assert result.result == "inconclusive"


def test_evidence_coverage_80_percent_reaches_normal_decision() -> None:
    """覆盖率恰好 80% 达到门槛，不因覆盖率拒判。"""
    sop = grinder_disassembly_sop()
    steps = [
        sop.steps[0].model_copy(update={"critical": False, "weight": 80.0}),
        sop.steps[1].model_copy(update={"critical": False, "weight": 20.0}),
    ]
    result = score_assessment(
        steps,
        [
            StepResult(
                step=steps[0],
                judgement="passed",
                rule_code="observed",
                observable_check_results={
                    check: "passed" for check in steps[0].observable_checks
                },
            )
        ],
    )
    assert result.evidence_coverage == 0.8
    assert result.result in {"pass", "fail"}


def test_unobservable_over_20_percent_inconclusive() -> None:
    """不可观察权重超过 20% → inconclusive，只显示临时分数与覆盖率。"""
    sop = grinder_disassembly_sop()
    results = _results_for(1, 2, 3, 4, 5, 6, 7)
    for r in results:
        if r.step.index in (3, 4):  # 权重 10+10=20，需 >20
            r.judgement = "unobservable"
    # 加上步骤 7 权重 10 → 30% 不可观察
    for r in results:
        if r.step.index == 7:
            r.judgement = "unobservable"
    result = score_assessment(sop.steps, results)
    assert result.result == "inconclusive"
    assert result.unobservable_weight > 20.0


def test_critical_unobservable_is_inconclusive_at_20_percent_boundary() -> None:
    """关键步本身不可观察时，即使权重恰好 20% 也不能通过。"""
    sop = grinder_disassembly_sop()
    results = _all_passed()
    results[0].judgement = "unobservable"  # step1 关键，权重 20
    result = score_assessment(sop.steps, results)
    assert result.unobservable_weight == 20.0
    assert result.result == "inconclusive"


def test_order_violation_deduces() -> None:
    """顺序错误：步骤 5 已通过但前置步骤 4 明确失败 → order 扣分。

    新语义：前置 insufficient_evidence（没识别到）不算顺序违规，只有前置
    failed（明确没做/做错）才扣分。
    """
    sop = grinder_disassembly_sop()
    results = _results_for(1, 2, 5)  # 5 前置 4 未提供（insufficient）
    # step4 改为明确 failed（操作者做错了 step4，却做了 step5 → 顺序违规）
    for r in results:
        if r.step.index == 4:
            r.judgement = "failed"
            r.rule_code = "wrong_tool"
            break
    result = score_assessment(sop.steps, results)
    order_score = result.dimension_scores["order"]
    full = score_assessment(sop.steps, _all_passed())
    assert order_score < full.dimension_scores["order"]


def test_order_insufficient_predecessor_not_penalized() -> None:
    """前置证据不足（没识别到）不判顺序违规——不惩罚漏检。"""
    sop = grinder_disassembly_sop()
    results = _results_for(1, 2, 5)  # step4 前置是 insufficient（未提供）
    result = score_assessment(sop.steps, results)
    # step5 passed, 前置 step4 是 insufficient → 不扣分, order=100
    assert result.dimension_scores["order"] == 100.0


def test_insufficient_evidence_not_fake_fail() -> None:
    """证据不足不判失败也不直接扣分——覆盖率反映。"""
    sop = grinder_disassembly_sop()
    results = []
    for step in sop.steps:
        results.append(
            StepResult(step=step, judgement="insufficient_evidence", rule_code="no_evidence")
        )
    result = score_assessment(sop.steps, results)
    assert result.evidence_coverage == 0.0
    assert result.result == "inconclusive"  # 无判定不伪判 pass/fail
    assert result.total_score == 0.0
    assert result.dimension_scores["order"] == 0.0
    assert result.dimension_scores["efficiency"] == 0.0
    # 无 failed 扣分项
    assert all(d.rule_code != "step_failed" for d in result.deductions)


def test_optional_missing_step_is_neutral_for_required_score() -> None:
    """可选步骤缺失不降低必做覆盖/完整性，也不会因 critical 标记触发拒判。"""
    sop = grinder_disassembly_sop()
    required = sop.steps[0].model_copy(
        update={
            "weight": 60.0,
            "critical": False,
            "expected_duration_s": 5,
        }
    )
    optional = sop.steps[1].model_copy(
        update={
            "weight": 40.0,
            "optional": True,
            "critical": True,
            "predecessors": [],
        }
    )
    results = [
        StepResult(
            step=required,
            judgement="passed",
            start_ms=1000,
            end_ms=6000,
            rule_code="observed",
            observable_check_results={
                check: "passed" for check in required.observable_checks
            },
        ),
        StepResult(
            step=optional,
            judgement="insufficient_evidence",
            rule_code="missing_evidence",
        ),
    ]

    result = score_assessment([required, optional], results)

    assert result.evidence_coverage == 1.0
    assert result.dimension_scores["completeness"] == 100.0
    assert result.critical_step_missing is False
    assert result.result == "pass"
    assert all(deduction.step_index != optional.index for deduction in result.deductions)


def test_point_evidence_does_not_receive_efficiency_score() -> None:
    sop = grinder_disassembly_sop()
    point_results = _all_passed()
    for result in point_results:
        result.end_ms = result.start_ms

    scored = score_assessment(sop.steps, point_results)

    assert scored.dimension_scores["efficiency"] == 0.0


def test_deterministic_same_input() -> None:
    """相同输入必须产生完全相同的分数（handoff §3.8）。"""
    sop = grinder_disassembly_sop()
    r1 = score_assessment(sop.steps, _all_passed())
    r2 = score_assessment(sop.steps, _all_passed())
    assert r1.total_score == r2.total_score
    assert r1.dimension_scores == r2.dimension_scores
    assert r1.result == r2.result


def test_applicable_dimensions_are_not_diluted_by_unrelated_steps() -> None:
    """不要求工具或安全检查的步骤不能降低这些维度的满分。"""
    sop = grinder_disassembly_sop()
    scored = score_assessment(sop.steps, _all_passed(), sop.scoring_profile)

    assert scored.dimension_scores["tools_parts"] == 100.0
    assert scored.dimension_scores["observable_safety"] == 100.0


def test_exact_mixed_check_results_override_incomplete_rule_fallback() -> None:
    step = grinder_disassembly_sop().steps[0].model_copy(update={"weight": 100.0})
    result = StepResult(
        step=step,
        judgement="passed",
        rule_code="observable_checks_incomplete",
        observable_check_results={
            step.observable_checks[0]: "passed",
            step.observable_checks[1]: "insufficient_evidence",
        },
    )

    scored = score_assessment([step], [result])

    assert scored.dimension_scores["observable_safety"] == 50.0
    assert scored.result == "inconclusive"


def test_exact_mixed_check_results_override_failed_rule_fallback() -> None:
    step = grinder_disassembly_sop().steps[0].model_copy(update={"weight": 100.0})
    result = StepResult(
        step=step,
        judgement="failed",
        rule_code="observable_check_failed",
        observable_check_results={
            step.observable_checks[0]: "passed",
            step.observable_checks[1]: "failed",
        },
    )

    scored = score_assessment([step], [result])

    assert scored.dimension_scores["observable_safety"] == 50.0
    assert scored.result == "fail"


def test_completion_evidence_is_not_scored_as_safety_or_a_pass_gate() -> None:
    step = SopStep(
        id="install-filter",
        index=1,
        name="安装滤芯",
        tools=["hand"],
        objects=["filter"],
        actions=["install"],
        completion_checks=["滤芯已就位"],
        critical=True,
        weight=100,
    )
    complete = StepResult(
        step=step,
        judgement="passed",
        rule_code="observed_checks_passed",
        observable_check_results={"滤芯已就位": "passed"},
    )
    incomplete = StepResult(
        step=step,
        judgement="passed",
        rule_code="completion_checks_incomplete",
        observable_check_results={"滤芯已就位": "insufficient_evidence"},
    )

    completed_score = score_assessment([step], [complete])
    incomplete_score = score_assessment([step], [incomplete])

    assert completed_score.dimension_scores["observable_safety"] == 100.0
    assert completed_score.result == "pass"
    assert incomplete_score.dimension_scores["observable_safety"] == 100.0
    assert incomplete_score.result == "pass"
    assert any(
        deduction.rule_code == "completion_evidence_insufficient"
        for deduction in incomplete_score.deductions
    )


def test_deductions_carry_evidence() -> None:
    """每个扣分项绑定规则代码与时间区间（handoff §10）。"""
    sop = grinder_disassembly_sop()
    results = _results_for(1, 2, 3, 4, 5, 6)  # 7 缺失
    result = score_assessment(sop.steps, results)
    missing = [d for d in result.deductions if d.step_index == 7]
    assert missing
    assert missing[0].rule_code
