"""审计路由：查询审计日志。admin 可查看全部，observer 可查看摘要。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gongxu.db.models import AuditAction, AuditLog, User, UserRole
from gongxu.db.session import get_db
from gongxu.security import auth

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditEntryOut(BaseModel):
    id: int
    actor_id: str | None
    action: str
    target_type: str | None
    target_id: str | None
    detail: dict[str, Any]
    ip_address: str
    created_at: datetime


class AuditSummaryOut(BaseModel):
    total: int
    by_action: dict[str, int]


@router.get("", response_model=list[AuditEntryOut])
def list_audit(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
    action: AuditAction | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditEntryOut]:
    if current_user.role not in (UserRole.admin, UserRole.observer):
        raise HTTPException(status_code=403, detail="权限不足")
    stmt = select(AuditLog)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    stmt = stmt.order_by(AuditLog.created_at.desc()).limit(limit)
    return [
        AuditEntryOut(
            id=e.id,
            actor_id=e.actor_id,
            action=e.action.value,
            target_type=e.target_type,
            target_id=e.target_id,
            detail=e.detail,
            ip_address=e.ip_address,
            created_at=e.created_at,
        )
        for e in db.scalars(stmt)
    ]


@router.get("/summary", response_model=AuditSummaryOut)
def audit_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> AuditSummaryOut:
    if current_user.role not in (UserRole.admin, UserRole.observer):
        raise HTTPException(status_code=403, detail="权限不足")
    rows = db.execute(
        select(AuditLog.action, func.count(AuditLog.id)).group_by(AuditLog.action)
    ).all()
    by_action = {action.value: count for action, count in rows}
    total = db.scalar(select(func.count(AuditLog.id))) or 0
    return AuditSummaryOut(total=total, by_action=by_action)
