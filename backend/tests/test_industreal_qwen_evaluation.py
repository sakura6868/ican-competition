"""IndustReal 官方标签到 Qwen 验证真值的确定性映射。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gongxu.evaluation.industreal_qwen import (
    _selection_suffix,
    _sop_steps,
    _truth_for_recording,
)
from gongxu.evaluation.qwen import EvaluationManifest


def test_assy_normal_sequence_maps_to_pass() -> None:
    labels = [
        (3, "Install front chassis"),
        (6, "Install front chassis pin"),
        (9, "Install rear chassis"),
        (15, "Install front rear chassis pin"),
        (18, "Install rear rear chassis pin"),
        (21, "Install front bracket"),
        (24, "Install front bracket screw"),
        (27, "Install front wheel assy"),
        (30, "Install rear wheel assy"),
    ]

    result, steps = _truth_for_recording("assy", labels)

    assert result == "pass"
    assert all(item["judgement"] == "passed" for item in steps)


def test_assy_official_error_and_correction_remain_failed() -> None:
    labels = [
        (3, "Install front chassis"),
        (6, "Install front chassis pin"),
        (9, "Install rear chassis"),
        (15, "Install front rear chassis pin"),
        (18, "Install rear rear chassis pin"),
        (22, "Incorrectly installed front bracket"),
        (25, "Incorrectly installed front bracket screw"),
        (21, "Install front bracket"),
        (24, "Install front bracket screw"),
        (27, "Install front wheel assy"),
        (30, "Install rear wheel assy"),
    ]

    result, steps = _truth_for_recording("assy", labels)
    failed = {item["step_index"] for item in steps if item["judgement"] == "failed"}

    assert result == "fail"
    assert failed == {6, 7}


def test_main_omitted_pin_maps_to_failed_step() -> None:
    labels = [
        (32, "Remove rear wheel assy"),
        (20, "Remove rear rear chassis pin"),
        (11, "Remove rear chassis"),
        (17, "Remove front rear chassis pin"),
        (12, "Install short rear chassis"),
        (18, "Install rear rear chassis pin"),
        (30, "Install rear wheel assy"),
    ]

    result, steps = _truth_for_recording("main", labels)
    failed = {item["step_index"] for item in steps if item["judgement"] == "failed"}

    assert result == "fail"
    assert failed == {6}


def test_sop_predecessors_preserve_parallel_completion_milestones() -> None:
    timestamps = {
        1: 19_800,
        2: 19_800,
        3: 55_700,
        4: 55_700,
        5: 63_100,
        6: 87_800,
        7: 87_800,
        8: 129_500,
        9: 176_500,
    }

    steps = _sop_steps(
        "assy",
        reference_asset_id="reference-asset",
        reference_timestamps=timestamps,
    )

    assert {step.index: step.predecessors for step in steps} == {
        1: [],
        2: [],
        3: [1, 2],
        4: [1, 2],
        5: [3, 4],
        6: [5],
        7: [5],
        8: [6, 7],
        9: [8],
    }
    assert all(step.completion_checks for step in steps)
    assert all(not step.observable_checks for step in steps)


def test_case_selection_suffix_is_stable_and_order_independent() -> None:
    first = _selection_suffix(["15_main_3_1", "01_assy_1_1"])
    second = _selection_suffix(["01_assy_1_1", "15_main_3_1", "15_main_3_1"])

    assert first == second
    assert first.startswith("-selected-")


def test_public_dataset_scope_requires_official_metadata_and_acknowledgement() -> None:
    payload = {
        "dataset_id": "official-val",
        "evaluation_role": "development_validation",
        "provider_export_authorized": True,
        "authorization_scope": ("official_public_dataset_license_plus_operator_acknowledgement"),
        "authorization_note": "official public dataset validation",
        "authorized_by_username": "admin",
        "process_key": "example",
        "sop_version_id": "sop-id",
        "cases": [
            {
                "case_id": "case-1",
                "video_path": "video.mp4",
                "sha256": "0" * 64,
                "authorization_note": "official licensed video",
                "expected_result": "pass",
                "expected_steps": [{"step_index": 1, "judgement": "passed"}],
            }
        ],
    }

    with pytest.raises(ValidationError, match="官方来源 URL"):
        EvaluationManifest.model_validate(payload)

    payload.update(
        {
            "official_source_url": "https://doi.org/example",
            "license_id": "Apache-2.0",
            "operator_acknowledged_provider_processing": True,
        }
    )
    parsed = EvaluationManifest.model_validate(payload)
    assert parsed.license_id == "Apache-2.0"
