"""CSRF 防护：双提交令牌（Double-Submit Cookie + Header）。

登录成功后下发 `gx_csrf` Cookie（非 HttpOnly，供 JS 读取），
状态变更请求需带 `X-CSRF-Token` 头，与 Cookie 值一致才放行。
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import HTTPException, Request, status

from gongxu.config import get_settings

CSRF_COOKIE = "gx_csrf"
CSRF_HEADER = "x-csrf-token"


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_cookie_options(max_age: int) -> dict[str, Any]:
    return {
        "key": CSRF_COOKIE,
        "value": new_csrf_token(),
        "max_age": max_age,
        "httponly": False,  # 前端 JS 需要读取以放入 header
        "samesite": "lax",  # 同源 SPA，lax 足够且兼容旧会话
        "secure": get_settings().cookie_secure,
        "path": "/",
    }


def require_csrf(request: Request) -> None:
    """FastAPI 依赖：校验双提交令牌；仅在配置启用时生效。"""
    settings = get_settings()
    if not settings.csrf_enabled:
        return
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败")


__all__ = ["CSRF_COOKIE", "CSRF_HEADER", "csrf_cookie_options", "require_csrf", "new_csrf_token"]
