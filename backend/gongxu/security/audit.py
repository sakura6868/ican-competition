"""审计日志：业务动作记录。

敏感信息（令牌、密码、密钥、完整原文）禁止写入 detail。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from gongxu.db.models import AuditAction, AuditLog


def record_audit(
    db: Session,
    *,
    action: AuditAction,
    actor_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    ip_address: str = "",
) -> AuditLog:
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail or {},
        ip_address=ip_address,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
