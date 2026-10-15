"""Setup must preserve existing encryption keys and existing local configuration."""

from types import SimpleNamespace

import pytest

from gongxu import tutor_setup


def test_first_setup_preserves_existing_development_key(tmp_path, monkeypatch):
    dev_key = tmp_path / "data" / "runtime" / "dev_master.key"
    dev_key.parent.mkdir(parents=True)
    dev_key.write_bytes(bytes(range(32)))
    (tmp_path / ".env.example").write_text(
        "GONGXU_SESSION_SECRET=change-me-in-production\nGONGXU_MASTER_KEY_HEX=\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tutor_setup, "PROJECT_ROOT", tmp_path)
    # Keep the actual cached function so its cache_clear remains available.
    monkeypatch.setenv("GONGXU_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("GONGXU_MASTER_KEY_HEX", "")
    tutor_setup.get_settings.cache_clear()
    tutor_setup.initialize_env()
    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"GONGXU_MASTER_KEY_HEX={bytes(range(32)).hex()}" in saved
    assert "change-me-in-production" not in saved
    tutor_setup.initialize_env()
    assert (tmp_path / ".env").read_text(encoding="utf-8") == saved


def test_setup_does_not_generate_new_key_over_unrecoverable_encrypted_assets(tmp_path, monkeypatch):
    from gongxu.config import get_settings

    encrypted = tmp_path / "assets" / "encrypted" / "original.bin"
    encrypted.parent.mkdir(parents=True)
    encrypted.write_bytes(b"existing encrypted source")
    monkeypatch.setattr(tutor_setup, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        tutor_setup, "get_settings", lambda: SimpleNamespace(data_root=tmp_path, master_key_hex="")
    )
    with pytest.raises(ValueError, match="恢复原密钥"):
        tutor_setup.initialize_env()
    assert not (tmp_path / ".env").exists()
    assert encrypted.read_bytes() == b"existing encrypted source"
    get_settings.cache_clear()
