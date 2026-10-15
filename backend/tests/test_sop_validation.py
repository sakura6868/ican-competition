"""SOP 业务校验测试：非法大模型输出无法入库。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gongxu.sop.fixtures import grinder_disassembly_sop
from gongxu.sop.schema import SopVersion, SourceGrade
from gongxu.sop.validation import SopValidationError, make_evidence, validate_sop


def test_valid_fixture_passes() -> None:
    sop = grinder_disassembly_sop()
    assert validate_sop(sop).version == 1
    assert len(sop.steps) == 7


def test_weight_sum_must_be_100() -> None:
    sop = grinder_disassembly_sop()
    sop.steps[0].weight = 10  # 把权重改到总和 < 100
    with pytest.raises(SopValidationError, match="权重总和"):
        validate_sop(sop)


def test_step_index_must_be_continuous() -> None:
    sop = grinder_disassembly_sop()
    sop.steps[-1].index = 99
    with pytest.raises(SopValidationError, match="编号必须连续"):
        validate_sop(sop)


def test_validate_and_store_normalizes_zero_based_index(db, video_asset) -> None:
    """Qwen 输出 index 从 0 开始 → 服务端归一化为从 1 连续编号（修复）。"""
    from gongxu.sop import service as sop_service
    from gongxu.sop.validation import make_evidence

    raw_sop = {
        "steps": [
            {"index": 0, "name": "第一步", "tools": ["power_plug"],
             "objects": ["power_outlet"], "actions": ["unplug"],
             "observable_checks": ["已断电"], "unobservable_checks": [],
             "weight": 50, "evidence_refs": [make_evidence("00:00-00:05")]},
            {"index": 1, "name": "第二步", "tools": ["wrench"],
             "objects": ["locking_nut"], "actions": ["loosen"],
             "predecessors": [0],
             "observable_checks": ["已松开"], "unobservable_checks": [],
             "weight": 50, "evidence_refs": [make_evidence("00:05-00:10")]},
        ]
    }
    row = sop_service._validate_and_store(
        db, raw_sop=raw_sop, receipt_id="receipt_test",
        process_key="impact_grinder_safe_subassembly", compiled_by=None,
        reference_video_asset_id=video_asset,
        source_asset_ids=[video_asset],
    )
    assert [s["index"] for s in row.steps] == [1, 2]
    assert row.steps[1]["predecessors"] == [1]  # predecessors 引用被重映射


def test_dag_cycle_rejected() -> None:
    sop = grinder_disassembly_sop()
    # 1 -> 3 且 3 -> 1 构成环
    sop.steps[0].predecessors = [3]
    sop.steps[2].predecessors = [1]
    with pytest.raises(SopValidationError, match="环"):
        validate_sop(sop)


def test_missing_predecessor_rejected() -> None:
    sop = grinder_disassembly_sop()
    sop.steps[2].predecessors = [42]
    with pytest.raises(SopValidationError, match="前置 42 不存在"):
        validate_sop(sop)


def test_missing_evidence_rejected() -> None:
    sop = grinder_disassembly_sop()
    sop.steps[0].evidence_refs = []
    with pytest.raises(SopValidationError, match="缺少来源证据"):
        validate_sop(sop)


def test_video_reference_cannot_use_manual_evidence() -> None:
    sop = grinder_disassembly_sop()
    sop.steps[0].evidence_refs = [make_evidence("手册p.12", kind="manual")]
    with pytest.raises(SopValidationError, match="与来源等级"):
        validate_sop(sop)


def test_manual_backed_allows_manual_evidence() -> None:
    sop = grinder_disassembly_sop()
    sop.source_grade = SourceGrade.manual_backed
    sop.steps[0].evidence_refs = [make_evidence("手册p.12", kind="manual")]
    assert validate_sop(sop).source_grade == SourceGrade.manual_backed


def test_manual_backed_requires_at_least_one_manual_evidence() -> None:
    sop = grinder_disassembly_sop()
    sop.source_grade = SourceGrade.manual_backed

    with pytest.raises(SopValidationError, match="至少需要一条 manual"):
        validate_sop(sop)


def test_sensor_verified_requires_sensor_evidence() -> None:
    sop = grinder_disassembly_sop()
    sop.source_grade = SourceGrade.sensor_verified

    with pytest.raises(SopValidationError, match="至少需要一条 sensor"):
        validate_sop(sop)

    sop.steps[0].evidence_refs.append(
        make_evidence("sensor#cal-01", kind="sensor", description="扭矩传感器校准记录")
    )
    assert validate_sop(sop).source_grade == SourceGrade.sensor_verified


def test_empty_evidence_reference_rejected() -> None:
    sop = grinder_disassembly_sop()
    sop.steps[0].evidence_refs = [make_evidence("   ")]

    with pytest.raises(SopValidationError, match="引用为空"):
        validate_sop(sop)


def test_step_without_checks_rejected() -> None:
    sop = grinder_disassembly_sop()
    sop.steps[0].observable_checks = []
    sop.steps[0].unobservable_checks = []
    with pytest.raises(SopValidationError, match="缺少通过条件"):
        validate_sop(sop)


def test_completion_check_is_a_valid_visual_condition() -> None:
    """完成状态可单独作为视频判定条件，不被迫伪装成安全规范。"""
    sop = grinder_disassembly_sop()
    sop.steps[0].completion_checks = ["组件已就位"]
    sop.steps[0].observable_checks = []
    assert validate_sop(sop).steps[0].completion_checks == ["组件已就位"]


def test_observable_step_without_semantic_signature_rejected() -> None:
    """纯检查文本无法和视频事件对齐，发布前必须补充结构化语义。"""
    sop = grinder_disassembly_sop()
    sop.steps[0].tools = []
    sop.steps[0].objects = []
    sop.steps[0].actions = []

    with pytest.raises(SopValidationError, match="缺少工具、对象或动作语义"):
        validate_sop(sop)


def test_empty_steps_rejected() -> None:
    sop = grinder_disassembly_sop()
    sop.steps = []
    with pytest.raises(SopValidationError, match="至少需要一个步骤"):
        validate_sop(sop)


def test_scoring_profile_sum() -> None:
    sop = grinder_disassembly_sop()
    sop.scoring_profile.completeness = 0.9
    with pytest.raises(SopValidationError, match="评分维度"):
        validate_sop(sop)


def test_invalid_schema_payload_rejected() -> None:
    """结构化输出字段缺失时 Pydantic 拒绝，无法入库。"""
    bad = {"process_key": "x", "steps": [{"id": "s1"}]}  # 缺 id/name/index 等必填
    with pytest.raises(ValidationError):
        SopVersion.model_validate(bad)
