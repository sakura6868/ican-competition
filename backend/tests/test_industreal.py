from __future__ import annotations

import io
import json
import zlib
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from gongxu.data import industreal
from gongxu.data.industreal import (
    IndustRealDownloadError,
    PilotRequest,
    _copy_atomically,
    _ensure_disk_space,
    _extract_requests,
    _read_split_recordings,
    _safe_destination,
    archive_url,
    development_requests,
    download_development,
    pilot_requests,
)


def test_pilot_plan_never_accesses_test() -> None:
    requests = pilot_requests()
    assert requests
    assert {request.split for request in requests} == {"train", "val"}
    assert all(not request.archive.startswith("test") for request in requests)
    assert sum(request.kind == "rgb_video" for request in requests) == 4
    assert sum(request.kind == "annotation" for request in requests) == 16


def test_metadata_only_plan_has_no_video() -> None:
    requests = pilot_requests(include_video=False)
    assert len(requests) == 16
    assert all(request.kind == "annotation" for request in requests)


def test_safe_destination_keeps_members_below_root(tmp_path: Path) -> None:
    request = PilotRequest("train", "01_assy_0_1", "train_p1", "01_assy_0_1/a.csv", "annotation")
    destination = _safe_destination(tmp_path, request)
    assert destination.is_relative_to(tmp_path)
    assert destination.name == "a.csv"


def test_safe_destination_rejects_traversal(tmp_path: Path) -> None:
    request = PilotRequest("train", "x", "train_p1", "../secret", "annotation")
    with pytest.raises(IndustRealDownloadError, match="unsafe archive member"):
        _safe_destination(tmp_path, request)


def test_atomic_copy_hashes_and_removes_partial(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "sample.bin"
    size, digest = _copy_atomically(io.BytesIO(b"official-data"), destination)
    assert size == len(b"official-data")
    assert len(digest) == 64
    assert destination.read_bytes() == b"official-data"
    assert not destination.with_suffix(".bin.part").exists()


class _TrackingStream(io.BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.seek_offsets: list[int] = []

    def seek(self, offset: int, whence: int = 0) -> int:
        self.seek_offsets.append(offset)
        return super().seek(offset, whence)


def test_atomic_copy_retains_and_resumes_verified_member_partial(tmp_path: Path) -> None:
    content = b"official-member-content"
    destination = tmp_path / "sample.bin"
    partial = destination.with_suffix(".bin.part")
    partial.write_bytes(content[:8])
    source = _TrackingStream(content)

    size, digest = _copy_atomically(
        source,
        destination,
        expected_size=len(content),
        expected_crc32=f"{zlib.crc32(content) & 0xFFFFFFFF:08x}",
    )

    assert size == len(content)
    assert digest == sha256(content).hexdigest()
    assert source.seek_offsets == [8]
    assert destination.read_bytes() == content
    assert not partial.exists()


def test_atomic_copy_keeps_incomplete_partial_for_next_attempt(tmp_path: Path) -> None:
    destination = tmp_path / "sample.bin"
    expected = b"complete-content"

    with pytest.raises(IndustRealDownloadError, match="partial retained"):
        _copy_atomically(
            io.BytesIO(expected[:5]),
            destination,
            expected_size=len(expected),
            expected_crc32=f"{zlib.crc32(expected) & 0xFFFFFFFF:08x}",
        )

    assert destination.with_suffix(".bin.part").read_bytes() == expected[:5]
    source = _TrackingStream(expected)
    _copy_atomically(
        source,
        destination,
        expected_size=len(expected),
        expected_crc32=f"{zlib.crc32(expected) & 0xFFFFFFFF:08x}",
    )
    assert source.seek_offsets == [5]
    assert destination.read_bytes() == expected


def test_atomic_copy_discards_complete_crc_mismatch(tmp_path: Path) -> None:
    destination = tmp_path / "sample.bin"
    expected = b"official"

    with pytest.raises(IndustRealDownloadError, match="corrupt partial was discarded"):
        _copy_atomically(
            io.BytesIO(b"bad-data"),
            destination,
            expected_size=len(expected),
            expected_crc32=f"{zlib.crc32(expected) & 0xFFFFFFFF:08x}",
        )

    assert not destination.exists()
    assert not destination.with_suffix(".bin.part").exists()


def test_atomic_copy_restarts_complete_bad_partial_from_zero(tmp_path: Path) -> None:
    destination = tmp_path / "sample.bin"
    partial = destination.with_suffix(".bin.part")
    partial.write_bytes(b"bad-data")
    expected = b"official"
    source = _TrackingStream(expected)

    _copy_atomically(
        source,
        destination,
        expected_size=len(expected),
        expected_crc32=f"{zlib.crc32(expected) & 0xFFFFFFFF:08x}",
    )

    assert source.seek_offsets == []
    assert destination.read_bytes() == expected
    assert not partial.exists()


def test_archive_url_only_accepts_allow_list() -> None:
    assert archive_url("rgb").startswith("https://data.4tu.nl/file/")
    assert archive_url("train_p4").endswith("3ff5409e-165f-4354-9fa4-a4fde639a7a6")
    assert archive_url("val_p2").endswith("e899de9f-313a-482b-a6b5-e2de4f38412e")
    with pytest.raises(IndustRealDownloadError, match="unknown"):
        archive_url("test_p1")


def test_official_v2_archive_md5_allow_list_is_fixed() -> None:
    assert industreal.OFFICIAL_ARCHIVE_MD5 == {
        "action_labels": "3a9c9e5b0060065edcadb0375b26e0ee",
        "rgb": "c5b8901dba179d2eb10e9348f8163c79",
        "train_p1": "b283383ee97cc92bd3410c09c3c1b4bc",
        "train_p2": "0d07877b0755b226c28b8c5952d6c0dd",
        "train_p3": "606509e026f501240595ff9805e9a662",
        "train_p4": "40193a8fee14fad551ff6ed7b140c29f",
        "val_p1": "a6d9464b6bbe5b7c4c2c05927ad95844",
        "val_p2": "0db912bd9565084e77090adabf78b7a4",
    }


def _development_fixture() -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, str]]]:
    split_recordings = {
        "train": tuple(f"train_{index:02d}" for index in range(36)),
        "val": tuple(f"val_{index:02d}" for index in range(16)),
    }
    recording_archives = {
        recording_id: (split, f"{split}_p1")
        for split, recording_ids in split_recordings.items()
        for recording_id in recording_ids
    }
    return split_recordings, recording_archives


def test_development_plan_has_52_videos_and_eight_label_types() -> None:
    split_recordings, recording_archives = _development_fixture()
    requests = development_requests(split_recordings, recording_archives)

    assert sum(request.kind == "rgb_video" for request in requests) == 52
    assert sum(request.kind != "rgb_video" for request in requests) == 52 * 8
    assert {request.split for request in requests} == {"train", "val"}
    assert all(not request.archive.startswith("test") for request in requests)
    assert {request.kind for request in requests} >= {
        "object_detection_labels",
        "hand_tracking",
        "gaze_tracking",
        "head_pose",
        "procedure_labels_with_errors",
    }


def test_development_metadata_only_plan_omits_video() -> None:
    split_recordings, recording_archives = _development_fixture()
    requests = development_requests(
        split_recordings,
        recording_archives,
        include_video=False,
    )
    assert len(requests) == 52 * 8
    assert all(request.kind != "rgb_video" for request in requests)


def test_development_plan_fails_closed_on_archive_mismatch() -> None:
    split_recordings, recording_archives = _development_fixture()
    recording_archives.pop("train_00")
    with pytest.raises(IndustRealDownloadError, match="split/archive mismatch"):
        development_requests(split_recordings, recording_archives)


def test_development_plan_rejects_test_archive() -> None:
    split_recordings, recording_archives = _development_fixture()
    recording_archives["train_00"] = ("train", "test_p1")
    with pytest.raises(IndustRealDownloadError, match="forbidden"):
        development_requests(split_recordings, recording_archives)


def test_read_split_recordings_uses_official_csv_ids(tmp_path: Path) -> None:
    path = tmp_path / "train.csv"
    rows = [f"recording_{index:02d},0,action,000001.jpg,000002.jpg" for index in range(36)]
    path.write_text("\n".join(rows), encoding="utf-8")
    recordings = _read_split_recordings(path, split="train")
    assert len(recordings) == 36
    assert recordings[0] == "recording_00"


def test_read_split_recordings_rejects_incomplete_official_split(tmp_path: Path) -> None:
    path = tmp_path / "val.csv"
    path.write_text("only_one,0,action,000001.jpg,000002.jpg\n", encoding="utf-8")
    with pytest.raises(IndustRealDownloadError, match="must contain 16"):
        _read_split_recordings(path, split="val")


def test_development_destination_is_separate_from_pilot(tmp_path: Path) -> None:
    request = PilotRequest(
        "train",
        "01_assy_0_1",
        "train_p1",
        "01_assy_0_1/hands.csv",
        "hand_tracking",
    )
    destination = _safe_destination(tmp_path, request, scope="development")
    assert destination == (
        tmp_path / "development" / "annotations" / "train" / "01_assy_0_1" / "hands.csv"
    )


def test_disk_preflight_counts_missing_files_and_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = PilotRequest("train", "one", "rgb", "one.mp4", "rgb_video")
    monkeypatch.setattr(
        industreal.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=1024**3 + 99),
    )
    with pytest.raises(IndustRealDownloadError, match="insufficient disk space"):
        _ensure_disk_space(
            tmp_path,
            [request],
            {("rgb", "one.mp4"): 100},
        )


def test_disk_preflight_reuses_complete_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = PilotRequest("train", "one", "rgb", "one.mp4", "rgb_video")
    destination = _safe_destination(tmp_path, request)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"complete")
    monkeypatch.setattr(
        industreal.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=1024**3),
    )
    missing = _ensure_disk_space(
        tmp_path,
        [request],
        {("rgb", "one.mp4"): len(b"complete")},
    )
    assert missing == 0


class _FakeRemoteArchive:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.opened: list[str] = []

    def __enter__(self) -> _FakeRemoteArchive:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def infolist(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                filename=name,
                file_size=len(content),
                CRC=zlib.crc32(content) & 0xFFFFFFFF,
            )
            for name, content in self.files.items()
        ]

    def open(self, name: str) -> io.BytesIO:
        self.opened.append(name)
        return io.BytesIO(self.files[name])


def test_extract_revalidates_same_size_local_file_with_official_crc32(tmp_path: Path) -> None:
    request = PilotRequest("train", "one", "rgb", "one.mp4", "rgb_video")
    destination = _safe_destination(tmp_path, request)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"bad-data")
    archive = _FakeRemoteArchive({"one.mp4": b"official"})

    files = _extract_requests(archive, [request], root=tmp_path)

    assert archive.opened == ["one.mp4"]
    assert destination.read_bytes() == b"official"
    assert files[0].crc32 == f"{zlib.crc32(b'official') & 0xFFFFFFFF:08x}"

    reused_archive = _FakeRemoteArchive({"one.mp4": b"official"})
    reused = _extract_requests(reused_archive, [request], root=tmp_path)
    assert reused_archive.opened == []
    assert reused[0].sha256 == files[0].sha256


def test_download_development_writes_complete_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    split_recordings, _recording_archives = _development_fixture()
    split_csvs = {
        f"{split}.csv": "\n".join(
            f"{recording_id},0,action,000001.jpg,000002.jpg" for recording_id in recording_ids
        ).encode()
        for split, recording_ids in split_recordings.items()
    }
    files_by_archive: dict[str, dict[str, bytes]] = {name: {} for name in industreal._ARCHIVES}
    files_by_archive["action_labels"] = split_csvs
    for split, recording_ids in split_recordings.items():
        archive_name = f"{split}_p1"
        for recording_id in recording_ids:
            files_by_archive["rgb"][f"{recording_id}.mp4"] = b"fake-mp4"
            for filename in industreal._DEVELOPMENT_LABEL_KINDS:
                files_by_archive[archive_name][f"{recording_id}/{filename}"] = b"label"

    by_url = {archive_url(name): files for name, files in files_by_archive.items()}
    monkeypatch.setattr(
        industreal,
        "_remote_zip_class",
        lambda: lambda url: _FakeRemoteArchive(by_url[url]),
    )
    monkeypatch.setattr(
        industreal.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=10 * 1024**3),
    )

    manifest_path = download_development(output_root=tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "industreal-development-v2"
    assert manifest["recording_counts"] == {"train": 36, "val": 16, "total": 52}
    assert manifest["test_archives_accessed"] is False
    assert manifest["test_split_member_extracted"] is False
    assert manifest["external_provider_export"] == "not_authorized_by_this_manifest"
    assert len(manifest["files"]) == 52 * 9 + 2
    assert {entry["kind"] for entry in manifest["files"]} >= {
        "rgb_video",
        "object_detection_labels",
        "hand_tracking",
        "gaze_tracking",
        "head_pose",
    }
    assert manifest["official_doi"] == industreal.DATASET_DOI
    assert manifest["source_archives"]["action_labels"]["md5"] == (
        "3a9c9e5b0060065edcadb0375b26e0ee"
    )
    assert manifest["source_archives"]["rgb"]["md5"] == ("c5b8901dba179d2eb10e9348f8163c79")
    assert all(len(entry["crc32"]) == 8 for entry in manifest["files"])
    assert manifest["download_recovery"]["remote_range_resume_guaranteed"] is False
    assert not list(tmp_path.rglob("*.part"))

    # A legacy manifest can be strengthened in place. All local bytes are
    # checked against central-directory CRC32, but no member is downloaded.
    manifest["schema_version"] = "industreal-development-v1"
    manifest.pop("download_recovery")
    for entry in manifest["files"]:
        entry.pop("crc32")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    reopened_archives: list[_FakeRemoteArchive] = []

    def reopen(url: str) -> _FakeRemoteArchive:
        archive = _FakeRemoteArchive(by_url[url])
        reopened_archives.append(archive)
        return archive

    monkeypatch.setattr(industreal, "_remote_zip_class", lambda: reopen)
    upgraded_path = download_development(output_root=tmp_path)
    upgraded = json.loads(upgraded_path.read_text(encoding="utf-8"))
    assert upgraded["schema_version"] == "industreal-development-v2"
    assert upgraded["planned_download_bytes_at_start"] == 0
    assert all(not archive.opened for archive in reopened_archives)
