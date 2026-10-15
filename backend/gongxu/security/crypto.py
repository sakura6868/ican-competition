"""加密模块：AES-256-GCM 数据加密与主密钥管理。

主密钥从环境变量或系统密钥库读取；生产 Windows 优先使用 DPAPI 包装。
本模块只负责加密原语与密钥获取，不涉及业务对象。
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gongxu.config import get_settings

_NONCE_LEN = 12


def _load_master_key() -> bytes:
    """从环境变量加载 32 字节主密钥；未配置时生成开发密钥。

    开发密钥只在本地测试/脚手架使用，生产必须配置 GONGXU_MASTER_KEY_HEX。
    """
    settings = get_settings()
    hex_key = settings.master_key_hex
    if hex_key:
        key = bytes.fromhex(hex_key)
        if len(key) != 32:
            raise ValueError("GONGXU_MASTER_KEY_HEX 必须是 64 个十六进制字符（32 字节）")
        return key
    dev_key_path = settings.data_root / "runtime" / "dev_master.key"
    if dev_key_path.exists():
        return dev_key_path.read_bytes()
    dev_key_path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    dev_key_path.write_bytes(key)
    # 开发密钥禁止进入 Git
    return key


def encrypt_bytes(plaintext: bytes) -> str:
    """加密返回 base64 编码的 nonce+ciphertext+tag，形如 gx1:<b64>。"""
    key = _load_master_key()
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return "gx1:" + base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_bytes(payload: str) -> bytes:
    """解密 `encrypt_bytes` 的输出；密钥错误或密文篡改时抛 InvalidTag。"""
    if not payload.startswith("gx1:"):
        raise ValueError("无法识别的密文前缀")
    raw = base64.b64decode(payload.removeprefix("gx1:"))
    nonce, ciphertext = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
    key = _load_master_key()
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise ValueError("密文校验失败（错误密钥或篡改）") from exc


def encrypt_text(plaintext: str) -> str:
    return encrypt_bytes(plaintext.encode("utf-8"))


def decrypt_text(payload: str) -> str:
    return decrypt_bytes(payload).decode("utf-8")


def generate_master_key_hex() -> str:
    """生成新的 64 位十六进制主密钥（用于初始化生产密钥）。"""
    return os.urandom(32).hex()
