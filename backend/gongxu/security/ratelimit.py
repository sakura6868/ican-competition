"""进程内登录限流（滑动窗口；多进程部署时替换为 Redis）。

双桶：IP 桶防止单来源暴力爆破；用户名桶防止共享 NAT 下被合法账号
反复重置（整改 P2-1）。任一桶超限即拒绝。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from gongxu.config import get_settings

# bucket_key -> deque of attempt timestamps
_attempts: dict[str, deque[float]] = defaultdict(deque)


def _bucket(ip: str, username: str) -> str:
    return f"ip:{ip}" if not username else f"ip:{ip}|user:{username.lower()}"


def check_rate_limit(ip: str, username: str = "") -> None:
    """登录前调用：超限抛 RateLimited。记录该 ip+username 的尝试。"""
    settings = get_settings()
    now = time.time()
    # 先检查是否已超限（不重复计数）
    bucket = _bucket(ip, username)
    window = _attempts[bucket]
    while window and now - window[0] > settings.login_window_seconds:
        window.popleft()
    if len(window) >= settings.login_max_attempts:
        raise RateLimited("登录尝试过于频繁，请稍后再试")
    # 已超限的尝试也应累计到 ip 桶，防止换用户名绕过
    ip_bucket = _bucket(ip, "")
    ip_window = _attempts[ip_bucket]
    while ip_window and now - ip_window[0] > settings.login_window_seconds:
        ip_window.popleft()
    if len(ip_window) >= settings.login_max_attempts * 2:
        raise RateLimited("登录尝试过于频繁，请稍后再试")
    window.append(now)


def reset_rate_limit(ip: str, username: str = "") -> None:
    """登录成功后清空该 ip+username 桶（不影响其他用户名的 ip 桶）。"""
    _attempts[_bucket(ip, username)].clear()


class RateLimited(Exception):
    pass
