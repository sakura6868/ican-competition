"""数据适配测试：SHA256、zip slip 防护、manifest 解析、split 一致性。"""

from __future__ import annotations

import json
import sys
import zipfile
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from gongxu.data.download import (
    IMPACT_RELEASE_REVISION,
    IntegrityError,
    download_bundle,
    verify_checksum,
)
from gongxu.data.manifest import (
    ImpactManifest,
    ManifestEntry,
    parse_sha256sums,
    sha256_file,
)


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def test_sha256_file_stable(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello impact" * 1000)
    assert sha256_file(f) == sha256_file(f)


def test_parse_sha256sums() -> None:
    text = (
        "abc123  annotations/IMPACT-v1.1-annotations.zip\ndef456 *sample/IMPACT-v1.1-sample.zip\n\n"
    )
    result = parse_sha256sums(text)
    assert result["annotations/IMPACT-v1.1-annotations.zip"] == "abc123"
    assert result["sample/IMPACT-v1.1-sample.zip"] == "def456"


def test_verify_checksum_match(tmp_path: Path) -> None:
    f = tmp_path / "x.zip"
    f.write_bytes(b"payload")
    expected = sha256_file(f)
    assert verify_checksum(f, expected) == expected


def test_verify_checksum_mismatch(tmp_path: Path) -> None:
    f = tmp_path / "x.zip"
    f.write_bytes(b"payload")
    with pytest.raises(IntegrityError, match="SHA256 不匹配"):
        verify_checksum(f, "0" * 64)


def test_verify_checksum_rejects_missing_expected_digest(tmp_path: Path) -> None:
    f = tmp_path / "x.zip"
    f.write_bytes(b"payload")
    with pytest.raises(IntegrityError, match="有效的预期 SHA256"):
        verify_checksum(f, "")


def test_download_uses_pinned_revision_and_official_checksum_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"official-archive"
    expected = sha256(payload).hexdigest()
    calls: list[dict[str, object]] = []

    def fake_hub_download(**kwargs: object) -> str:
        calls.append(kwargs)
        filename = str(kwargs["filename"])
        path = Path(str(kwargs["local_dir"])) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if filename == "v1.1/SHA256SUMS":
            path.write_text(
                f"{expected} annotations/IMPACT-v1.1-annotations.zip\n",
                encoding="utf-8",
            )
        else:
            path.write_bytes(payload)
        return str(path)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(hf_hub_download=fake_hub_download),
    )

    downloaded = download_bundle("annotations", output_dir=tmp_path)

    assert downloaded.read_bytes() == payload
    assert [call["filename"] for call in calls] == [
        "v1.1/SHA256SUMS",
        "v1.1/annotations/IMPACT-v1.1-annotations.zip",
    ]
    assert all(call["revision"] == IMPACT_RELEASE_REVISION for call in calls)


def test_download_fails_closed_when_official_checksum_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_hub_download(**kwargs: object) -> str:
        filename = str(kwargs["filename"])
        if filename != "v1.1/SHA256SUMS":
            pytest.fail("archive must not download without an expected checksum")
        path = Path(str(kwargs["local_dir"])) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("0" * 64 + " other.zip\n", encoding="utf-8")
        return str(path)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(hf_hub_download=fake_hub_download),
    )

    with pytest.raises(IntegrityError, match="未提供"):
        download_bundle("annotations", output_dir=tmp_path)


def test_safe_extract_blocks_zip_slip(tmp_path: Path) -> None:
    from gongxu.data.download import _safe_extract

    evil = tmp_path / "evil.zip"
    _write_zip(evil, {"../escape.txt": b"pwn"})
    with pytest.raises(IntegrityError, match="路径穿越"):
        _safe_extract(evil, tmp_path / "dest")


def test_safe_extract_ok(tmp_path: Path) -> None:
    from gongxu.data.download import _safe_extract

    z = tmp_path / "ok.zip"
    _write_zip(z, {"IMPACT-v1.1/metadata/a.txt": b"ok"})
    dest = tmp_path / "dest"
    _safe_extract(z, dest)
    assert (dest / "IMPACT-v1.1/metadata/a.txt").read_bytes() == b"ok"


def test_manifest_roundtrip(tmp_path: Path) -> None:
    m = ImpactManifest(
        entries=[
            ManifestEntry(
                identifier="AL07EJ17_Disassembly_A_001_front",
                view="front",
                split="S1",
                redistributable=False,
            )
        ],
        checksums={"a.zip": "abc"},
    )
    raw = m.model_dump()
    reloaded = ImpactManifest.model_validate(json.loads(json.dumps(raw)))
    assert reloaded.entries[0].identifier == m.entries[0].identifier
    assert reloaded.entries[0].redistributable is False


def test_bundle_keys_cover_official_layout() -> None:
    from gongxu.data.manifest import BUNDLES

    assert "annotations" in BUNDLES
    assert "sample" in BUNDLES
    assert "videos:front" in BUNDLES
    assert "features:VideoMAEv2" in BUNDLES
    assert "depth:top" in BUNDLES
