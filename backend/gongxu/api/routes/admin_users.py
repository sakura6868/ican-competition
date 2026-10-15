"""管理员路由：用户管理。仅 admin 可访问。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gongxu.db.models import AuditAction, User, UserRole
from gongxu.db.session import get_db
from gongxu.security import auth
from gongxu.security.audit import record_audit
from gongxu.security.csrf import require_csrf

router = APIRouter(prefix="/admin", tags=["admin"])

require_admin = auth.require_roles(UserRole.admin)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=128)
    role: UserRole


class UserUpdateRequest(BaseModel):
    display_name: str | None = None
    role: UserRole | None = None
    is_active: bool | None = None


class UserAdminOut(BaseModel):
    id: str
    username: str
    display_name: str
    role: str
    is_active: bool


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _out(user: User) -> UserAdminOut:
    return UserAdminOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role.value,
        is_active=user.is_active,
    )


@router.get("/users", response_model=list[UserAdminOut])
def list_users(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> list[UserAdminOut]:
    return [_out(u) for u in db.scalars(select(User).order_by(User.created_at))]


@router.post(
    "/users",
    response_model=UserAdminOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
def create_user(
    body: UserCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> UserAdminOut:
    try:
        user = auth.create_user(
            db,
            username=body.username,
            password=body.password,
            role=body.role,
            display_name=body.display_name,
        )
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit(
        db,
        action=AuditAction.user_create,
        actor_id=admin.id,
        target_type="user",
        target_id=user.id,
        detail={"username": user.username, "role": user.role.value},
        ip_address=_client_ip(request),
    )
    return _out(user)


@router.patch("/users/{user_id}", response_model=UserAdminOut, dependencies=[Depends(require_csrf)])
def update_user(
    user_id: str,
    body: UserUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> UserAdminOut:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if body.display_name is not None:
        user.display_name = body.display_name
    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    db.commit()
    db.refresh(user)
    record_audit(
        db,
        action=AuditAction.user_update,
        actor_id=admin.id,
        target_type="user",
        target_id=user.id,
        detail={"role": user.role.value, "is_active": user.is_active},
        ip_address=_client_ip(request),
    )
    return _out(user)
