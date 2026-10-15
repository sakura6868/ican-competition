"""加密模块测试：AES-256-GCM 往返、错误密钥、篡改检测。"""

from __future__ import annotations

import pytest

from gongxu.security import crypto


def test_encrypt_decrypt_roundtrip(db) -> None:
    text = "安全装配工序：先断电，再固定工件，最后检查打磨片。"
    payload = crypto.encrypt_text(text)
    assert payload.startswith("gx1:")
    assert crypto.decrypt_text(payload) == text


def test_encrypt_binary_roundtrip(db) -> None:
    blob = bytes(range(256))
    payload = crypto.encrypt_bytes(blob)
    assert crypto.decrypt_bytes(payload) == blob


def test_same_plaintext_different_ciphertext(db) -> None:
    """每次加密 nonce 不同，密文不应相同。"""
    a = crypto.encrypt_text("same-value")
    b = crypto.encrypt_text("same-value")
    assert a != b
    assert crypto.decrypt_text(a) == crypto.decrypt_text(b) == "same-value"


def test_wrong_key_fails(db) -> None:
    payload = crypto.encrypt_text("secret")
    # 换一个主密钥后解密必须失败
    crypto._load_master_key.cache_clear() if hasattr(
        crypto._load_master_key, "cache_clear"
    ) else None
    import os

    original = os.environ.get("GONGXU_MASTER_KEY_HEX")
    os.environ["GONGXU_MASTER_KEY_HEX"] = "11" * 32
    crypto.get_settings.cache_clear() if hasattr(crypto, "get_settings") else None
    from gongxu.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError):
            crypto.decrypt_text(payload)
    finally:
        if original is None:
            os.environ.pop("GONGXU_MASTER_KEY_HEX", None)
        else:
            os.environ["GONGXU_MASTER_KEY_HEX"] = original
        get_settings.cache_clear()


def test_tampered_ciphertext_fails(db) -> None:
    payload = crypto.encrypt_text("sensitive")
    raw_prefix, b64 = payload.split(":", 1)
    import base64

    raw = bytearray(base64.b64decode(b64))
    raw[-1] ^= 0x01  # 翻转密文最后一个字节
    tampered = raw_prefix + ":" + base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(ValueError):
        crypto.decrypt_text(tampered)


def test_unknown_prefix_rejected(db) -> None:
    with pytest.raises(ValueError):
        crypto.decrypt_text("zz9:aaaa")


def test_master_key_hex_validation(db) -> None:
    import os

    from gongxu.config import get_settings

    original = os.environ.get("GONGXU_MASTER_KEY_HEX")
    os.environ["GONGXU_MASTER_KEY_HEX"] = "abc"  # 长度错误
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError):
            crypto._load_master_key()
    finally:
        if original is None:
            os.environ.pop("GONGXU_MASTER_KEY_HEX", None)
        else:
            os.environ["GONGXU_MASTER_KEY_HEX"] = original
        get_settings.cache_clear()
