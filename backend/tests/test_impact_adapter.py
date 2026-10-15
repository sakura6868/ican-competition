"""IMPACT v2 adapter tests use compact schema-faithful fixtures, never raw videos."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gongxu.data.impact_adapter import (
    ImpactAdapterError,
    ImpactV2Adapter,
    inclusive_frames_to_half_open_ms,
)


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _tas_s(identifier: str, *, fps: float = 30.0) -> dict[str, object]:
    return {
        "video_id": identifier,
        "view": identifier.rsplit("_", 1)[1],
        "meta_data": {"fps": fps, "num_frames": 60},
        "segments": [
            {
                "id": 1,
                "label": "unscrew_anti_vibration_handle",
                "has_anomaly": False,
                "meta_activity": "normal",
                "f_start": 0,
                "f_end": 29,
            },
            {
                "id": 2,
                "label": "null",
                "has_anomaly": False,
                "meta_activity": "none",
                "f_start": 30,
                "f_end": 59,
            },
        ],
    }


def test_inclusive_frames_become_half_open_milliseconds() -> None:
    assert inclusive_frames_to_half_open_ms(0, 0, 25.0) == (0, 40)
    assert inclusive_frames_to_half_open_ms(24, 24, 25.0) == (960, 1000)
    assert inclusive_frames_to_half_open_ms(0, 29, 30.0) == (0, 1000)
    with pytest.raises(ImpactAdapterError, match="frame bounds"):
        inclusive_frames_to_half_open_ms(5, 4, 30.0)
    with pytest.raises(ImpactAdapterError, match="fps"):
        inclusive_frames_to_half_open_ms(0, 1, 0.0)


def test_full_layout_tas_s_and_unmapped_label_are_explicit(tmp_path: Path) -> None:
    identifier = "AL07EJ17_Disassembly_A_001_front"
    release = tmp_path / "IMPACT-v1.1"
    annotation = _write_json(
        release / "annotations" / "TAS-S" / "front" / f"{identifier}.json",
        _tas_s(identifier),
    )
    split = release / "annotations" / "TAS-S" / "splits" / "val.split2.bundle"
    split.parent.mkdir(parents=True, exist_ok=True)
    split.write_text(identifier + "\n", encoding="utf-8")
    media = release / "videos" / "front" / f"{identifier}.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"selected-video")

    adapter = ImpactV2Adapter(
        tmp_path,
        label_mapping={"unscrew_anti_vibration_handle": 1},
        split_protocol="S2",
    )
    record = adapter.load(identifier, "tas_s")

    assert record.participant == "AL07EJ17"
    assert record.execution == "AL07EJ17_Disassembly_A_001"
    assert record.view == "front"
    assert record.task == "Disassembly"
    assert record.protocol == "TAS-S"
    assert record.split == "val"
    assert record.media_available is True
    assert record.annotation_available is True
    assert record.annotation_path == str(annotation.resolve())
    assert (record.segments[0].start_ms, record.segments[0].end_ms) == (0, 1000)
    assert record.segments[0].scoreable is True
    assert record.segments[0].sop_step_index == 1
    assert record.segments[1].scoreable is False
    assert record.segments[1].sop_step_index is None
    assert record.sha256 == {}  # Auto-discovered files are not implicitly hashed.


def test_sample_layout_tas_b_hashes_only_explicit_selected_files(tmp_path: Path) -> None:
    identifier = "ER07AD15_Disassembly_A_001_top"
    sample = tmp_path / "IMPACT-v1.1" / "sample"
    annotation = _write_json(
        sample / "annotations" / "TAS-B" / "top" / f"{identifier}.json",
        {
            "video_id": identifier,
            "view": "top",
            "meta_data": {"fps": 20.0, "num_frames": 20},
            "action_labels": [
                {"id": 0, "name": "null"},
                {"id": 7, "name": "loosen_screw"},
            ],
            "verbs": [{"id": 12, "name": "loosen"}],
            "nouns": [{"id": 13, "name": "screw"}],
            "anomaly_types": [
                {"id": 0, "name": "error_temporal"},
                {"id": 1, "name": "error_wrong_tool"},
            ],
            "segments": [
                {
                    "action_label": 7,
                    "verb": 12,
                    "noun": 13,
                    "start_frame": 10,
                    "end_frame": 19,
                    "phase": "anomaly",
                    "anomaly_type": [0, 1],
                    "entity": "right",
                },
                {
                    "action_label": 0,
                    "verb": -1,
                    "noun": -1,
                    "start_frame": 0,
                    "end_frame": 9,
                    "phase": "normal",
                    "anomaly_type": [0, 0],
                    "entity": "left",
                },
            ],
        },
    )
    media = sample / "videos" / "top" / f"{identifier}.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"rgb")

    record = ImpactV2Adapter(
        sample,
        label_mapping={"loosen_screw": 4},
        allow_held_out_test=True,
    ).load(identifier, "TAS-B", media_path=media, annotation_path=annotation, split="test")

    assert record.split == "test"
    assert set(record.sha256) == {"media", "annotation"}
    assert all(len(value) == 64 for value in record.sha256.values())
    background, segment = record.segments
    assert (background.label, background.verb, background.noun) == ("null", "", "")
    assert background.scoreable is False
    assert (segment.start_ms, segment.end_ms) == (500, 1000)
    assert (segment.label, segment.verb, segment.noun, segment.hand) == (
        "loosen_screw",
        "loosen",
        "screw",
        "right",
    )
    assert segment.anomaly_types == ["error_wrong_tool"]
    assert segment.has_anomaly is True
    assert segment.scoreable is True


def test_psr_infers_split_and_fps_from_matching_tas_s(tmp_path: Path) -> None:
    identifier = "KE03ER16_Reassembly_A_003_front"
    release = tmp_path / "IMPACT-v1.1"
    _write_json(
        release / "annotations" / "TAS-S" / "front" / f"{identifier}.json",
        _tas_s(identifier, fps=20.0),
    )
    psr = release / "annotations" / "PSR" / "labels" / "val" / identifier / "PSR_labels.csv"
    psr.parent.mkdir(parents=True, exist_ok=True)
    psr.write_text(
        "10.jpg,2,Remove anti_vibration_handle\n20.jpg,44,Remove lever\n",
        encoding="utf-8",
    )

    record = ImpactV2Adapter(release, label_mapping={"Remove anti_vibration_handle": 1}).load(
        identifier, "psr"
    )

    assert record.protocol == "PSR"
    assert record.split == "val"
    assert record.fps == 20.0
    assert (record.segments[0].start_ms, record.segments[0].end_ms) == (500, 550)
    assert record.segments[0].scoreable is True
    assert record.segments[1].scoreable is False
    assert record.media_available is False
    assert record.sha256 == {}


def test_psr_with_errors_is_explicit_variant(tmp_path: Path) -> None:
    identifier = "KE03ER16_Reassembly_A_003_front"
    release = tmp_path / "IMPACT-v1.1"
    _write_json(
        release / "annotations" / "TAS-S" / "front" / f"{identifier}.json",
        _tas_s(identifier, fps=20.0),
    )
    psr = (
        release
        / "annotations"
        / "PSR"
        / "labels"
        / "val"
        / identifier
        / "PSR_labels_with_errors.csv"
    )
    psr.parent.mkdir(parents=True, exist_ok=True)
    psr.write_text(
        "10.jpg,2,Remove anti_vibration_handle\n"
        "20.jpg,44,Incorrectly installed lever\n",
        encoding="utf-8",
    )

    record = ImpactV2Adapter(release).load(
        identifier,
        "PSR",
        psr_variant="with_errors",
    )

    assert record.annotation_variant == "with_errors"
    assert record.annotation_path == str(psr.resolve())
    assert record.segments[0].has_anomaly is False
    assert record.segments[1].has_anomaly is True
    assert record.segments[1].anomaly_types == ["incorrect_assembly"]


def test_psr_held_out_test_is_invisible_unless_explicitly_allowed(tmp_path: Path) -> None:
    identifier = "KE03ER16_Reassembly_A_003_front"
    release = tmp_path / "IMPACT-v1.1"
    psr = release / "annotations" / "PSR" / "labels" / "test" / identifier / "PSR_labels.csv"
    psr.parent.mkdir(parents=True, exist_ok=True)
    psr.write_text("10.jpg,2,Remove anti_vibration_handle\n", encoding="utf-8")

    default_record = ImpactV2Adapter(release).load(identifier, "PSR", fps=20.0)
    assert default_record.annotation_available is False
    assert default_record.annotation_path == ""
    assert default_record.split == ""

    with pytest.raises(ImpactAdapterError, match="allow_held_out_test=True"):
        ImpactV2Adapter(release).load(identifier, "PSR", fps=20.0, split="test")

    allowed_record = ImpactV2Adapter(release, allow_held_out_test=True).load(
        identifier, "PSR", fps=20.0
    )
    assert allowed_record.annotation_path == str(psr.resolve())
    assert allowed_record.annotation_available is True
    assert allowed_record.split == "test"


def test_tas_held_out_membership_is_rejected_before_annotation_read(
    tmp_path: Path,
) -> None:
    identifier = "AL07EJ17_Disassembly_A_001_front"
    release = tmp_path / "IMPACT-v1.1"
    annotation = _write_json(
        release / "annotations" / "TAS-S" / "front" / f"{identifier}.json",
        _tas_s(identifier),
    )
    split = release / "annotations" / "TAS-S" / "splits" / "test.split1.bundle"
    split.parent.mkdir(parents=True, exist_ok=True)
    split.write_text(identifier + "\n", encoding="utf-8")

    with pytest.raises(ImpactAdapterError, match="allow_held_out_test=True"):
        ImpactV2Adapter(release).load(identifier, "TAS-S")

    record = ImpactV2Adapter(release, allow_held_out_test=True).load(identifier, "TAS-S")
    assert record.annotation_path == str(annotation.resolve())
    assert record.split == "test"


def test_missing_selected_files_report_unavailable_without_hashing(tmp_path: Path) -> None:
    identifier = "KI03AR28_Disassembly_A_001_left"
    missing_media = tmp_path / "missing.mp4"
    missing_annotation = tmp_path / "missing.json"

    record = ImpactV2Adapter(tmp_path).load(
        identifier,
        "TAS-S",
        media_path=missing_media,
        annotation_path=missing_annotation,
    )

    assert record.media_available is False
    assert record.annotation_available is False
    assert record.segments == []
    assert record.sha256 == {}


def test_mismatched_annotation_identity_is_rejected(tmp_path: Path) -> None:
    identifier = "AL07EJ17_Disassembly_A_001_front"
    annotation = _write_json(
        tmp_path / "IMPACT-v1.1" / "annotations" / "TAS-S" / "front" / f"{identifier}.json",
        _tas_s("ER07AD15_Disassembly_A_001_front"),
    )
    with pytest.raises(ImpactAdapterError, match="video_id"):
        ImpactV2Adapter(tmp_path).load(identifier, "TAS-S", annotation_path=annotation)


def test_ambiguous_split_membership_is_rejected(tmp_path: Path) -> None:
    identifier = "AL07EJ17_Disassembly_A_001_front"
    release = tmp_path / "IMPACT-v1.1"
    _write_json(
        release / "annotations" / "TAS-S" / "front" / f"{identifier}.json",
        _tas_s(identifier),
    )
    split_root = release / "annotations" / "TAS-S" / "splits"
    split_root.mkdir(parents=True, exist_ok=True)
    for split in ("train", "test"):
        (split_root / f"{split}.split1.bundle").write_text(identifier + "\n", encoding="utf-8")

    with pytest.raises(ImpactAdapterError, match="multiple split1 partitions"):
        ImpactV2Adapter(tmp_path).load(identifier, "TAS-S")


def test_invalid_mapping_and_psr_without_fps_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ImpactAdapterError, match="positive SOP steps"):
        ImpactV2Adapter(tmp_path, label_mapping={"label": 0})

    identifier = "AL07EJ17_Disassembly_A_001_front"
    psr = tmp_path / "annotations" / "PSR" / "labels" / "train" / identifier / "PSR_labels.csv"
    psr.parent.mkdir(parents=True, exist_ok=True)
    psr.write_text("1.jpg,2,Remove anti_vibration_handle\n", encoding="utf-8")
    with pytest.raises(ImpactAdapterError, match="requires fps"):
        ImpactV2Adapter(tmp_path).load(identifier, "PSR")
