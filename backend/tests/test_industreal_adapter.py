"""IndustReal adapter tests use tiny fixtures, never downloaded pilot media."""

from __future__ import annotations

import json
import zlib
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from gongxu.data.industreal import DATASET_DOI, OFFICIAL_ARCHIVE_MD5, archive_url
from gongxu.data.industreal_adapter import (
    IndustRealAdapterError,
    IndustRealPilotAdapter,
    inclusive_10fps_frames_to_half_open_ms,
    load_industreal_pilot,
)


def _write(path: Path, content: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _entry(
    pilot: Path,
    *,
    split: str,
    recording_id: str,
    kind: str,
    path: Path,
) -> dict[str, Any]:
    relative = path.relative_to(pilot.parent).as_posix()
    source_member = f"{recording_id}.mp4" if kind == "rgb_video" else f"{recording_id}/{path.name}"
    return {
        "split": split,
        "recording_id": recording_id,
        "kind": kind,
        "source_archive": "rgb" if kind == "rgb_video" else f"{split}_p1",
        "source_member": source_member,
        "relative_path": relative,
        "bytes": path.stat().st_size,
        "sha256": sha256(path.read_bytes()).hexdigest(),
    }


def _build_pilot(
    tmp_path: Path,
    *,
    split: str = "train",
    recording_id: str = "01_assy_0_1",
) -> tuple[Path, dict[str, Any]]:
    pilot = tmp_path / "industreal-v2" / "pilot"
    media = _write(pilot / "media" / split / f"{recording_id}.mp4", b"fixture-video")
    annotation_root = pilot / "annotations" / split / recording_id
    annotations = {
        "AR_labels.csv": _write(
            annotation_root / "AR_labels.csv",
            f"{recording_id},5,tighten_nut,000010.jpg,000019.jpg\n"
            f"{recording_id},7,check_instruction,000020.jpg,000021.jpg\n",
        ),
        "PSR_labels.csv": _write(
            annotation_root / "PSR_labels.csv",
            "000020.jpg,3,Install wheel\n",
        ),
        "PSR_labels_with_errors.csv": _write(
            annotation_root / "PSR_labels_with_errors.csv",
            "000020.jpg,3,Install wheel\n000030.jpg,31,Incorrectly installed bracket\n",
        ),
        "PSR_labels_raw.csv": _write(
            annotation_root / "PSR_labels_raw.csv",
            "000000.jpg,1,0,0\n000020.jpg,1,1,0\n000030.jpg,1,1,-1\n",
        ),
    }
    files = [
        _entry(
            pilot,
            split=split,
            recording_id=recording_id,
            kind="annotation",
            path=path,
        )
        for path in annotations.values()
    ]
    files.append(
        _entry(
            pilot,
            split=split,
            recording_id=recording_id,
            kind="rgb_video",
            path=media,
        )
    )
    manifest: dict[str, Any] = {
        "schema_version": "industreal-pilot-v1",
        "dataset_id": "industreal",
        "dataset_version": "v2",
        "test_archives_accessed": False,
        "files": files,
    }
    manifest_path = pilot / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return pilot, manifest


def _write_manifest(pilot: Path, manifest: dict[str, Any]) -> None:
    (pilot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _upgrade_to_v2(pilot: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    manifest["schema_version"] = "industreal-pilot-v2"
    manifest["official_doi"] = DATASET_DOI
    for entry in manifest["files"]:
        path = pilot.parent / entry["relative_path"]
        entry["crc32"] = f"{zlib.crc32(path.read_bytes()) & 0xFFFFFFFF:08x}"
    referenced = {entry["source_archive"] for entry in manifest["files"]}
    manifest["source_archives"] = {
        name: {
            "url": archive_url(name),
            "role": "rgb_video" if name == "rgb" else "per_recording_annotations",
            "md5": OFFICIAL_ARCHIVE_MD5[name],
        }
        for name in sorted(referenced)
    }
    _write_manifest(pilot, manifest)
    return manifest


def _as_development_manifest(
    root: Path, manifest: dict[str, Any], *, split: str, recording_id: str
) -> Path:
    development = root.parent / "development"
    old_root = root
    for source in sorted(old_root.rglob("*")):
        if not source.is_file() or source.name == "manifest.json":
            continue
        relative = source.relative_to(old_root)
        target = development / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())

    kind_by_name = {
        "AR_labels.csv": "action_labels",
        "PSR_labels.csv": "procedure_labels",
        "PSR_labels_raw.csv": "procedure_state_raw",
        "PSR_labels_with_errors.csv": "procedure_labels_with_errors",
    }
    files: list[dict[str, Any]] = []
    for original in manifest["files"]:
        entry = dict(original)
        old_relative = Path(entry["relative_path"])
        entry["relative_path"] = (
            Path("development") / old_relative.relative_to("pilot")
        ).as_posix()
        if entry["kind"] == "annotation":
            entry["kind"] = kind_by_name[Path(entry["relative_path"]).name]
        files.append(entry)

    annotation_root = development / "annotations" / split / recording_id
    optional = {
        "OD_labels.json": ("object_detection_labels", "{}"),
        "hands.csv": ("hand_tracking", "000000.jpg,0,0\n"),
        "gaze.csv": ("gaze_tracking", "000000.jpg,0,0\n"),
        "pose.csv": ("head_pose", "000000.jpg,0,0,0\n"),
    }
    for filename, (kind, content) in optional.items():
        path = _write(annotation_root / filename, content)
        files.append(
            _entry(
                development,
                split=split,
                recording_id=recording_id,
                kind=kind,
                path=path,
            )
        )

    split_path = _write(
        development / "source_labels" / f"{split}.csv",
        f"{recording_id},5,tighten_nut,000010.jpg,000019.jpg\n",
    )
    files.append(
        {
            "split": split,
            "recording_id": "__split__",
            "kind": "split_action_labels",
            "source_archive": "action_labels",
            "source_member": f"{split}.csv",
            "relative_path": split_path.relative_to(development.parent).as_posix(),
            "bytes": split_path.stat().st_size,
            "sha256": sha256(split_path.read_bytes()).hexdigest(),
        }
    )
    other_split = "val" if split == "train" else "train"
    other_path = _write(development / "source_labels" / f"{other_split}.csv", "")
    files.append(
        {
            "split": other_split,
            "recording_id": "__split__",
            "kind": "split_action_labels",
            "source_archive": "action_labels",
            "source_member": f"{other_split}.csv",
            "relative_path": other_path.relative_to(development.parent).as_posix(),
            "bytes": 0,
            "sha256": sha256(b"").hexdigest(),
        }
    )
    payload = {
        "schema_version": "industreal-development-v1",
        "dataset_id": "industreal",
        "dataset_version": "v2",
        "recording_counts": {
            "train": int(split == "train"),
            "val": int(split == "val"),
            "total": 1,
        },
        "test_archives_accessed": False,
        "test_split_member_extracted": False,
        "external_provider_export": "not_authorized_by_this_manifest",
        "files": files,
    }
    (development / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    return development


def test_frames_are_converted_to_half_open_10fps_milliseconds() -> None:
    assert inclusive_10fps_frames_to_half_open_ms(0, 0) == (0, 100)
    assert inclusive_10fps_frames_to_half_open_ms(10, 19) == (1000, 2000)
    with pytest.raises(IndustRealAdapterError, match="frame bounds"):
        inclusive_10fps_frames_to_half_open_ms(3, 2)


def test_complete_pilot_is_verified_parsed_and_explicitly_mapped(tmp_path: Path) -> None:
    pilot, _ = _build_pilot(tmp_path)

    dataset = load_industreal_pilot(
        pilot,
        label_mapping={"tighten_nut": 2, "Install wheel": 3},
    )

    assert dataset.dataset_id == "industreal"
    assert dataset.dataset_version == "v2"
    assert dataset.manifest_bytes == (pilot / "manifest.json").stat().st_size
    assert dataset.manifest_sha256 == sha256((pilot / "manifest.json").read_bytes()).hexdigest()
    assert len(dataset.records) == 1
    record = dataset.records[0]
    assert record.recording_id == "01_assy_0_1"
    assert record.split == "train"
    assert record.media_sha256 == sha256(b"fixture-video").hexdigest()
    assert set(record.annotation_sha256) == {
        "AR_labels.csv",
        "PSR_labels.csv",
        "PSR_labels_raw.csv",
        "PSR_labels_with_errors.csv",
    }
    assert (record.actions[0].start_ms, record.actions[0].end_ms) == (1000, 2000)
    assert record.actions[0].scoreable is True
    assert record.actions[0].sop_step_index == 2
    assert record.actions[1].scoreable is False
    assert record.reference_completions[0].scoreable is True
    assert record.reference_completions[0].sop_step_index == 3
    assert record.completion_events[1].status == "incorrect"
    assert record.completion_events[1].scoreable is False
    assert record.completion_events[1].timestamp_ms == 3000
    assert record.completion_events[1].end_ms == 3100
    assert record.raw_states[-1].component_states == [1, 1, -1]
    assert record.raw_states[-1].incorrect_component_indices == [2]
    assert record.raw_states[-1].has_error is True


def test_strengthened_manifest_verifies_official_archive_provenance_and_crc32(
    tmp_path: Path,
) -> None:
    pilot, manifest = _build_pilot(tmp_path)
    _upgrade_to_v2(pilot, manifest)

    dataset = load_industreal_pilot(pilot)

    assert len(dataset.records) == 1


def test_strengthened_manifest_rejects_wrong_official_archive_md5(tmp_path: Path) -> None:
    pilot, manifest = _build_pilot(tmp_path)
    _upgrade_to_v2(pilot, manifest)
    manifest["source_archives"]["rgb"]["md5"] = "0" * 32
    _write_manifest(pilot, manifest)

    with pytest.raises(IndustRealAdapterError, match="official archive MD5 mismatch"):
        load_industreal_pilot(pilot)


def test_strengthened_crc_rejects_same_size_file_with_self_signed_sha256(
    tmp_path: Path,
) -> None:
    pilot, manifest = _build_pilot(tmp_path)
    _upgrade_to_v2(pilot, manifest)
    media = pilot / "media" / "train" / "01_assy_0_1.mp4"
    media.write_bytes(b"altered-video")
    media_entry = next(item for item in manifest["files"] if item["kind"] == "rgb_video")
    media_entry["sha256"] = sha256(media.read_bytes()).hexdigest()
    _write_manifest(pilot, manifest)

    with pytest.raises(IndustRealAdapterError, match="CRC32 mismatch"):
        load_industreal_pilot(pilot)


def test_strengthened_manifest_requires_member_crc32(tmp_path: Path) -> None:
    pilot, manifest = _build_pilot(tmp_path)
    _upgrade_to_v2(pilot, manifest)
    manifest["files"][0].pop("crc32")
    _write_manifest(pilot, manifest)

    with pytest.raises(IndustRealAdapterError, match="crc32 is required"):
        load_industreal_pilot(pilot)


def test_development_manifest_verifies_optional_modalities_and_split_membership(
    tmp_path: Path,
) -> None:
    pilot, manifest = _build_pilot(tmp_path, split="val", recording_id="05_assy_2_2")
    development = _as_development_manifest(
        pilot,
        manifest,
        split="val",
        recording_id="05_assy_2_2",
    )

    dataset = load_industreal_pilot(development)

    assert len(dataset.records) == 1
    assert set(dataset.records[0].annotation_sha256) == {
        "AR_labels.csv",
        "PSR_labels.csv",
        "PSR_labels_raw.csv",
        "PSR_labels_with_errors.csv",
        "OD_labels.json",
        "hands.csv",
        "gaze.csv",
        "pose.csv",
    }


def test_development_manifest_rejects_external_export_or_split_drift(
    tmp_path: Path,
) -> None:
    pilot, manifest = _build_pilot(tmp_path, split="val", recording_id="05_assy_2_2")
    development = _as_development_manifest(
        pilot,
        manifest,
        split="val",
        recording_id="05_assy_2_2",
    )
    payload = json.loads((development / "manifest.json").read_text(encoding="utf-8"))
    payload["external_provider_export"] = "allowed"
    (development / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IndustRealAdapterError, match="external provider export"):
        load_industreal_pilot(development)

    payload["external_provider_export"] = "not_authorized_by_this_manifest"
    split_file = development / "source_labels" / "val.csv"
    split_file.write_text(
        "different_recording,5,tighten_nut,000010.jpg,000019.jpg\n",
        encoding="utf-8",
    )
    split_entry = next(
        item
        for item in payload["files"]
        if item["kind"] == "split_action_labels" and item["split"] == "val"
    )
    split_entry["bytes"] = split_file.stat().st_size
    split_entry["sha256"] = sha256(split_file.read_bytes()).hexdigest()
    (development / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IndustRealAdapterError, match="membership"):
        load_industreal_pilot(development)


@pytest.mark.parametrize("field", ["bytes", "sha256"])
def test_manifest_size_and_sha256_are_verified(tmp_path: Path, field: str) -> None:
    pilot, manifest = _build_pilot(tmp_path)
    media = next(item for item in manifest["files"] if item["kind"] == "rgb_video")
    media[field] = media[field] + 1 if field == "bytes" else "0" * 64
    _write_manifest(pilot, manifest)

    with pytest.raises(IndustRealAdapterError, match="byte size mismatch|SHA256 mismatch"):
        IndustRealPilotAdapter(pilot).load()


def test_media_tampering_with_same_size_is_rejected(tmp_path: Path) -> None:
    pilot, _ = _build_pilot(tmp_path)
    media = pilot / "media" / "train" / "01_assy_0_1.mp4"
    media.write_bytes(b"altered-video")

    with pytest.raises(IndustRealAdapterError, match="SHA256 mismatch"):
        IndustRealPilotAdapter(pilot).load()


def test_missing_annotation_breaks_media_pairing(tmp_path: Path) -> None:
    pilot, manifest = _build_pilot(tmp_path)
    manifest["files"] = [
        item
        for item in manifest["files"]
        if not item["relative_path"].endswith("PSR_labels_raw.csv")
    ]
    _write_manifest(pilot, manifest)

    with pytest.raises(IndustRealAdapterError, match="annotation pairing failed"):
        IndustRealPilotAdapter(pilot).load()


def test_annotation_recording_mismatch_is_rejected(tmp_path: Path) -> None:
    pilot, manifest = _build_pilot(tmp_path)
    action = pilot / "annotations" / "train" / "01_assy_0_1" / "AR_labels.csv"
    action.write_text(
        "02_assy_0_1,5,tighten_nut,000010.jpg,000019.jpg\n",
        encoding="utf-8",
    )
    entry = next(
        item for item in manifest["files"] if item["relative_path"].endswith("AR_labels.csv")
    )
    entry["bytes"] = action.stat().st_size
    entry["sha256"] = sha256(action.read_bytes()).hexdigest()
    _write_manifest(pilot, manifest)

    with pytest.raises(IndustRealAdapterError, match="recording .* != manifest"):
        IndustRealPilotAdapter(pilot).load()


@pytest.mark.parametrize("manifest_test_flag", [False, True])
def test_test_material_is_forbidden(tmp_path: Path, manifest_test_flag: bool) -> None:
    split = "train" if manifest_test_flag else "test"
    pilot, manifest = _build_pilot(tmp_path, split=split, recording_id="03_assy_0_1")
    if manifest_test_flag:
        manifest["test_archives_accessed"] = True
    _write_manifest(pilot, manifest)

    with pytest.raises(IndustRealAdapterError, match="test"):
        IndustRealPilotAdapter(pilot).load()


def test_with_errors_must_retain_reference_completions(tmp_path: Path) -> None:
    pilot, manifest = _build_pilot(tmp_path)
    annotation = pilot / "annotations" / "train" / "01_assy_0_1" / "PSR_labels_with_errors.csv"
    annotation.write_text(
        "000030.jpg,31,Incorrectly installed bracket\n",
        encoding="utf-8",
    )
    entry = next(
        item
        for item in manifest["files"]
        if item["relative_path"].endswith("PSR_labels_with_errors.csv")
    )
    entry["bytes"] = annotation.stat().st_size
    entry["sha256"] = sha256(annotation.read_bytes()).hexdigest()
    _write_manifest(pilot, manifest)

    with pytest.raises(IndustRealAdapterError, match="omits 1 reference"):
        IndustRealPilotAdapter(pilot).load()


def test_invalid_mapping_and_raw_state_are_rejected(tmp_path: Path) -> None:
    pilot, manifest = _build_pilot(tmp_path)
    with pytest.raises(IndustRealAdapterError, match="positive integer SOP steps"):
        IndustRealPilotAdapter(pilot, label_mapping={"tighten_nut": 0})

    raw = pilot / "annotations" / "train" / "01_assy_0_1" / "PSR_labels_raw.csv"
    raw.write_text("000000.jpg,1,2,0\n", encoding="utf-8")
    entry = next(
        item for item in manifest["files"] if item["relative_path"].endswith("PSR_labels_raw.csv")
    )
    entry["bytes"] = raw.stat().st_size
    entry["sha256"] = sha256(raw.read_bytes()).hexdigest()
    _write_manifest(pilot, manifest)

    with pytest.raises(IndustRealAdapterError, match="must be -1, 0, or 1"):
        IndustRealPilotAdapter(pilot).load_recording("01_assy_0_1")
