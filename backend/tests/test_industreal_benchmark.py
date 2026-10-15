"""Local-only IndustReal development benchmark manifest tests."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from gongxu.data.industreal_adapter import (
    IndustRealCompletionEvent,
    IndustRealPilot,
    IndustRealRecord,
)
from gongxu.ml.industreal_benchmark import (
    IndustRealBenchmarkError,
    build_industreal_benchmark,
    load_benchmark_manifest,
    write_benchmark_outputs,
)


def _event(
    *, frame: int, step_id: int, label: str, status: str
) -> IndustRealCompletionEvent:
    return IndustRealCompletionEvent(
        step_id=step_id,
        label=label,
        frame=frame,
        timestamp_ms=frame * 100,
        end_ms=(frame + 1) * 100,
        status=status,
    )


def _record(
    recording_id: str,
    *,
    split: str,
    events: list[IndustRealCompletionEvent],
    media_token: str,
) -> IndustRealRecord:
    return IndustRealRecord(
        recording_id=recording_id,
        split=split,
        media_path=f"/private/local/{recording_id}.mp4",
        media_bytes=123,
        media_sha256=sha256(media_token.encode()).hexdigest(),
        annotation_sha256={},
        actions=[],
        reference_completions=[],
        completion_events=events,
        raw_states=[],
    )


def _pilot(*records: IndustRealRecord) -> IndustRealPilot:
    return IndustRealPilot(
        dataset_id="industreal",
        dataset_version="v2",
        manifest_path="/private/local/manifest.json",
        manifest_bytes=456,
        manifest_sha256="a" * 64,
        records=list(records),
    )


def test_builder_is_deterministic_and_uses_val_only() -> None:
    train = _record(
        "01_assy_0_1",
        split="train",
        events=[_event(frame=5, step_id=1, label="Train event", status="correct")],
        media_token="train",
    )
    val = _record(
        "05_assy_2_2",
        split="val",
        events=[
            _event(frame=30, step_id=31, label="Incorrectly installed bracket", status="incorrect"),
            _event(frame=20, step_id=3, label="Install wheel", status="correct"),
        ],
        media_token="val",
    )

    first = build_industreal_benchmark(_pilot(train, val))
    second = build_industreal_benchmark(_pilot(val, train))

    assert first == second
    assert {case.recording_id for case in first.cases} == {"05_assy_2_2"}
    assert all(case.split == "val" for case in first.cases)
    assert first.summary.recording_count == 1
    assert first.summary.participant_count == 1
    assert first.summary.case_count == 2


def test_error_events_and_windows_are_preserved() -> None:
    val = _record(
        "05_assy_2_2",
        split="val",
        events=[
            _event(frame=10, step_id=31, label="Incorrectly installed bracket", status="incorrect"),
            _event(frame=40, step_id=3, label="Install wheel", status="correct"),
        ],
        media_token="val-errors",
    )

    manifest = build_industreal_benchmark(
        _pilot(val), context_pre_roll_ms=2_000, context_post_roll_ms=1_000
    )

    error = next(case for case in manifest.cases if case.ground_truth_status == "incorrect")
    assert error.ground_truth_label == "Incorrectly installed bracket"
    assert error.event_timestamp_ms == 1_000
    assert (error.window_start_ms, error.window_end_ms) == (0, 2_100)
    assert manifest.summary.ground_truth_distribution == {"correct": 1, "incorrect": 1}


def test_manifest_is_explicitly_local_only_and_carries_no_media_path() -> None:
    val = _record(
        "07_assy_0_1",
        split="val",
        events=[_event(frame=20, step_id=3, label="Install wheel", status="correct")],
        media_token="private-video",
    )

    manifest = build_industreal_benchmark(_pilot(val))
    payload = manifest.model_dump(mode="json")

    assert payload["evaluation_role"] == "development_validation"
    assert payload["formal_accuracy_claim_allowed"] is False
    assert payload["external_provider_export"] == "blocked"
    assert payload["external_provider_calls"] == 0
    assert payload["raw_frames_included"] is False
    assert all(case["external_provider_export"] == "blocked" for case in payload["cases"])
    assert "/private/local" not in str(payload)


def test_json_and_markdown_outputs_are_loadable_and_auditable(tmp_path: Path) -> None:
    val = _record(
        "05_assy_0_1",
        split="val",
        events=[_event(frame=20, step_id=3, label="Install | wheel", status="correct")],
        media_token="val-output",
    )
    manifest = build_industreal_benchmark(_pilot(val))
    json_path = tmp_path / "nested" / "benchmark.json"
    markdown_path = tmp_path / "nested" / "benchmark.md"

    written = write_benchmark_outputs(
        manifest, json_path=json_path, markdown_path=markdown_path
    )

    assert written == (json_path, markdown_path)
    assert load_benchmark_manifest(json_path) == manifest
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "不允许据此宣称正式准确率" in markdown
    assert "`blocked`" in markdown
    assert "Install \\| wheel" in markdown
    assert "/private/local" not in markdown


def test_empty_val_is_rejected_instead_of_falling_back_to_train() -> None:
    train = _record(
        "01_assy_0_1",
        split="train",
        events=[_event(frame=5, step_id=1, label="Train event", status="correct")],
        media_token="train-only",
    )

    with pytest.raises(IndustRealBenchmarkError, match="no val recordings"):
        build_industreal_benchmark(_pilot(train))


def test_val_without_completion_events_is_rejected() -> None:
    val = _record("05_assy_0_1", split="val", events=[], media_token="empty-val")

    with pytest.raises(IndustRealBenchmarkError, match="no completion events"):
        build_industreal_benchmark(_pilot(val))
