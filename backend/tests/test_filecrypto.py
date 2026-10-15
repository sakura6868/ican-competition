"""文件分块加解密测试。"""

from __future__ import annotations

import os

import pytest

from gongxu.security import filecrypto


def test_file_roundtrip_large(tmp_path) -> None:
    """跨多块（>1MiB）的往返加密解密。"""
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(3 * 1024 * 1024 + 12345))
    enc = tmp_path / "big.enc"
    dec = tmp_path / "big.out"
    filecrypto.encrypt_file(src, enc)
    assert enc.stat().st_size > src.stat().st_size
    filecrypto.decrypt_file(enc, dec)
    assert dec.read_bytes() == src.read_bytes()


def test_file_roundtrip_small(tmp_path) -> None:
    src = tmp_path / "small.txt"
    src.write_text("安全装配工序说明", encoding="utf-8")
    enc = tmp_path / "small.enc"
    dec = tmp_path / "small.out"
    filecrypto.encrypt_file(src, enc)
    filecrypto.decrypt_file(enc, dec)
    assert dec.read_text(encoding="utf-8") == "安全装配工序说明"


def test_tampered_file_fails(tmp_path) -> None:
    src = tmp_path / "a.bin"
    src.write_bytes(b"payload" * 100)
    enc = tmp_path / "a.enc"
    filecrypto.encrypt_file(src, enc)
    # 篡改密文末尾
    data = bytearray(enc.read_bytes())
    data[-1] ^= 0xFF
    enc.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="校验失败"):
        filecrypto.decrypt_file(enc, tmp_path / "out.bin")


def test_dev_key_fallback_when_env_unset(tmp_path, monkeypatch) -> None:
    """未配置 GONGXU_MASTER_KEY_HEX 时使用本地开发密钥，仍可往返。"""
    monkeypatch.setenv("GONGXU_MASTER_KEY_HEX", "")
    from gongxu.config import get_settings

    get_settings.cache_clear()
    try:
        src = tmp_path / "x.txt"
        src.write_text("开发模式", encoding="utf-8")
        enc = tmp_path / "x.enc"
        out = tmp_path / "x.out"
        filecrypto.encrypt_file(src, enc)
        filecrypto.decrypt_file(enc, out)
        assert out.read_text(encoding="utf-8") == "开发模式"
    finally:
        get_settings.cache_clear()
