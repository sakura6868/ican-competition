"""SOP 语义质量门禁测试（整改 P0-2）：无关工具/对象、缺证据、缺名称全部拒绝。"""

from __future__ import annotations

import pytest

from gongxu.sop.vocab import SopSemanticError, check_sop_semantics, check_step_semantics


def _step(**overrides):
    base = {
        "id": "s1",
        "index": 1,
        "name": "断开电源",
        "predecessors": [],
        "optional": False,
        "critical": True,
        "tools": ["wrench"],
        "objects": ["locking_nut"],
        "actions": ["loosen"],
        "expected_duration_s": 10,
        "observable_checks": ["完成"],
        "unobservable_checks": [],
        "weight": 100,
        "evidence_refs": [{"kind": "video", "ref": "00:03-00:09"}],
    }
    base.update(overrides)
    return base


def test_valid_grinder_step_passes() -> None:
    check_step_semantics("impact_grinder_safe_subassembly", _step())


def test_wrong_tool_rejected() -> None:
    """审核例：角磨机工序出现无关工具（如 Video Signal Generator）→ 拒绝。"""
    with pytest.raises(SopSemanticError, match="工具"):
        check_step_semantics(
            "impact_grinder_safe_subassembly",
            _step(tools=["Video Signal Generator"]),
        )


def test_wrong_object_rejected() -> None:
    with pytest.raises(SopSemanticError, match="对象"):
        check_step_semantics(
            "impact_grinder_safe_subassembly",
            _step(objects=["vectorscope"]),
        )


def test_wrong_action_rejected() -> None:
    with pytest.raises(SopSemanticError, match="动作"):
        check_step_semantics(
            "impact_grinder_safe_subassembly",
            _step(actions=["calibrate"]),
        )


def test_multi_step_whole_rejected() -> None:
    """整个 SOP 任一步骤语义失败即整体拒绝。"""
    steps = [_step(), _step(id="s2", index=2, tools=["waveform_monitor"])]
    with pytest.raises(SopSemanticError):
        check_sop_semantics("impact_grinder_safe_subassembly", steps)


def test_unregistered_process_vocab_is_rejected() -> None:
    """新工序没有受控词表时必须显式阻断，不能自由放行。"""
    with pytest.raises(SopSemanticError, match="尚未登记受控词表"):
        check_step_semantics("gearbox_disassembly_demo", _step(tools=["arbitrary_tool"]))
