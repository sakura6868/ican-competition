"""Tests for the one-command official dataset preparation workflow."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from gongxu.data.prepare import DatasetPreparationError, prepare_industreal_pilot
from gongxu.media.video import VideoInfo


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _entry(*, data_root: Path, path: Path, split: str, recording_id: str) -> dict[str, Any]:
    relative = path.relative_to(data_root / "raw" / "industreal-v2")
    annotation = path.suffix == ".csv"
    return {
        "split": split,
        "recording_id": recording_id,
        "kind": "annotation" if annotation else "rgb_video",
        "source_archive": "train_p1" if annotation else "rgb",
        "source_member": (
            f"{recording_id}/{path.name}" if annotation else f"{recording_id}.mp4"
        ),
        "relative_path": str(relative),
        "bytes": path.stat().st_size,
        "sha256": sha256(path.read_bytes()).hexdigest(),
    }


def _recording(data_root: Path, split: str, recording_id: str) -> list[dict[str, Any]]:
    pilot = data_root / "raw" / "industreal-v2" / "pilot"
    media = _write(pilot / "media" / split / f"{recording_id}.mp4", b"fake-mp4")
    labels = pilot / "annotations" / split / recording_id
    files = [
        media,
        _write(labels / "AR_labels.csv", f"{recording_id},1,fit_wheel,0.jpg,9.jpg\n".encode()),
        _write(labels / "PSR_labels.csv", b"9.jpg,1,Install wheel\n"),
        _write(
            labels / "PSR_labels_with_errors.csv",
            b"9.jpg,1,Install wheel\n"
            b"10.jpg,2,Incorrectly installed wheel\n"
            b"11.jpg,2,Incorrectly installed wheel\n",
        ),
        _write(labels / "PSR_labels_raw.csv", b"0.jpg,0,0\n10.jpg,1,-1\n"),
    ]
    return [
        _entry(data_root=data_root, path=path, split=split, recording_id=recording_id)
        for path in files
    ]


def _pilot(data_root: Path) -> Path:
    files = _recording(data_root, "train", "01_assy_0_1")
    files += _recording(data_root, "val", "05_assy_2_2")
    manifest = {
        "schema_version": "industreal-pilot-v1",
        "dataset_id": "industreal",
        "dataset_version": "v2",
        "test_archives_accessed": False,
        "files": files,
    }
    path = data_root / "raw" / "industreal-v2" / "pilot" / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_prepare_reuses_and_verifies_local_pilot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    manifest = _pilot(data_root)
    original = manifest.read_bytes()
    monkeypatch.setattr(
        "gongxu.data.prepare.probe",
        lambda _path: VideoInfo(duration_s=2.0, width=1280, height=720, fps=10.0, codec="h264"),
    )

    report = prepare_industreal_pilot(
        data_root=data_root, report_dir=tmp_path / "reports", allow_download=False
    )

    assert manifest.read_bytes() == original
    assert report["summary"] == {
        "recordings": 2,
        "split_counts": {"train": 1, "val": 1},
        "duration_seconds": 4.0,
        "action_segments": 2,
        "reference_completion_events": 2,
        "completion_events": 6,
        "incorrect_completion_events": 4,
    }
    assert report["test_archives_accessed"] is False
    assert report["source_manifest_schema_version"] == "industreal-pilot-v1"
    assert report["official_member_integrity"] == "legacy_local_sha256_only"
    assert report["formal_metric_claim_allowed"] is False
    assert report["external_provider_export"].startswith("blocked")
    assert all(item["incorrect_completion_events"] == 2 for item in report["records"])
    assert (tmp_path / "reports" / "industreal-pilot-readiness.json").is_file()
    assert (tmp_path / "reports" / "dataset-readiness.md").is_file()


def test_prepare_offline_rejects_missing_data(tmp_path: Path) -> None:
    with pytest.raises(DatasetPreparationError, match="offline mode"):
        prepare_industreal_pilot(
            data_root=tmp_path / "data",
            report_dir=tmp_path / "reports",
            allow_download=False,
        )


def test_prepare_rejects_annotation_beyond_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    _pilot(data_root)
    monkeypatch.setattr(
        "gongxu.data.prepare.probe",
        lambda _path: VideoInfo(duration_s=0.01, width=1280, height=720, fps=10.0, codec="h264"),
    )

    with pytest.raises(DatasetPreparationError, match="annotations exceed video duration"):
        prepare_industreal_pilot(
            data_root=data_root,
            report_dir=tmp_path / "reports",
            allow_download=False,
        )
