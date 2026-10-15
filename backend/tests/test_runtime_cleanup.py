"""Privacy cleanup for abandoned plaintext runtime workspaces."""

from __future__ import annotations

from pathlib import Path

import pytest

from gongxu.config import get_settings
from gongxu.runtime_cleanup import (
    cleanup_api_request_temps,
    cleanup_worker_startup_temps,
)


def _write(path: Path, content: bytes = b"plaintext") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_api_startup_cleanup_is_limited_to_request_temp_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GONGXU_MODEL_ROOT", str(tmp_path / "models"))
    get_settings.cache_clear()
    settings = get_settings()
    runtime = settings.data_root / "runtime"
    leftovers = [
        _write(runtime / "uploads" / "upload_dead.mov"),
        _write(runtime / "previews" / "preview_dead" / "decrypted.mp4"),
        _write(runtime / "evidence-previews" / "evidence_dead" / "frame.jpg"),
    ]
    protected = [
        _write(runtime / "dev_master.key", b"key"),
        _write(runtime / "backend.log", b"log"),
        _write(settings.data_root / "assets" / "encrypted" / "asset.bin", b"cipher"),
        _write(settings.data_root / "derivatives" / "encrypted" / "derived.bin", b"cipher"),
        _write(settings.data_root / "gongxu.db", b"database"),
        _write(settings.model_root / "detector.onnx", b"model"),
    ]

    report = cleanup_api_request_temps(settings)

    assert report.errors == []
    assert report.entries_removed == 3
    assert all(not path.exists() for path in leftovers)
    assert all(path.exists() for path in protected)


def test_worker_startup_cleanup_removes_only_worker_workspaces() -> None:
    settings = get_settings()
    runtime = settings.data_root / "runtime"
    leftovers = [
        _write(runtime / "work" / "job_dead" / "plain" / "attempt.mp4"),
        _write(runtime / "sop-compile" / "process_dead" / "plain" / "reference.mp4"),
    ]
    protected = [
        _write(runtime / "uploads" / "active-upload.tmp"),
        _write(runtime / "previews" / "active-preview" / "plain.mp4"),
        _write(runtime / "worker.log", b"log"),
        _write(settings.data_root / "assets" / "encrypted" / "asset.bin", b"cipher"),
    ]

    report = cleanup_worker_startup_temps(settings)

    assert report.errors == []
    assert report.entries_removed == 2
    assert all(not path.exists() for path in leftovers)
    assert all(path.exists() for path in protected)


def test_cleanup_unlinks_symlink_without_following_target(tmp_path: Path) -> None:
    settings = get_settings()
    external = _write(tmp_path / "outside" / "private.mp4", b"must-survive")
    upload_root = settings.data_root / "runtime" / "uploads"
    upload_root.mkdir(parents=True)
    link = upload_root / "escaped.mov"
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("platform does not permit symlink creation")

    report = cleanup_api_request_temps(settings)

    assert report.errors == []
    assert report.symlinks_removed == 1
    assert not link.exists()
    assert not link.is_symlink()
    assert external.read_bytes() == b"must-survive"


def test_cleanup_rejects_symlinked_allowlist_root(tmp_path: Path) -> None:
    settings = get_settings()
    external = tmp_path / "outside-previews"
    secret = _write(external / "plain.mp4", b"must-survive")
    preview_root = settings.data_root / "runtime" / "previews"
    preview_root.parent.mkdir(parents=True)
    try:
        preview_root.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("platform does not permit symlink creation")

    report = cleanup_api_request_temps(settings)

    assert any(item["reason"] == "symlinked_root_rejected" for item in report.errors)
    assert preview_root.is_symlink()
    assert secret.read_bytes() == b"must-survive"


@pytest.mark.asyncio
async def test_api_lifespan_cleans_before_database_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gongxu import main as main_module

    calls: list[str] = []
    monkeypatch.setattr(
        main_module,
        "cleanup_api_request_temps",
        lambda settings: calls.append("cleanup"),
    )
    monkeypatch.setattr(main_module, "init_db", lambda settings: calls.append("database"))
    app = main_module.create_app()

    async with app.router.lifespan_context(app):
        assert calls == ["cleanup", "database"]


def test_worker_main_cleans_before_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    from gongxu import worker

    calls: list[str] = []
    monkeypatch.setattr(
        "gongxu.runtime_cleanup.cleanup_worker_startup_temps",
        lambda settings: calls.append("cleanup"),
    )
    monkeypatch.setattr(worker, "init_db", lambda: calls.append("database"))
    monkeypatch.setattr(worker, "get_session_factory", lambda: object())
    monkeypatch.setattr(worker, "_claim_and_run", lambda factory: calls.append("claim") or False)
    monkeypatch.setattr("sys.argv", ["gongxu.worker", "--once"])

    assert worker.main() == 0
    assert calls == ["cleanup", "database", "claim"]
