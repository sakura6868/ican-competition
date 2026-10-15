"""密码哈希与随机会话令牌。

密码使用 Argon2（argon2-cffi）；会话令牌是 32 字节随机值，数据库只存 SHA-256 摘要。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def generate_token() -> str:
    """生成 32 字节随机会话令牌。"""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """只存摘要，不存原始令牌。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def timing_safe_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
