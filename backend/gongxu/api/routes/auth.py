"""认证路由：登录、登出、当前用户、改密。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.db.models import AuditAction, User
from gongxu.db.session import get_db
from gongxu.security import auth
from gongxu.security.audit import record_audit
from gongxu.security.csrf import CSRF_COOKIE, csrf_cookie_options, require_csrf
from gongxu.security.ratelimit import RateLimited, check_rate_limit, reset_rate_limit

router = APIRouter(tags=["auth"])

SESSION_COOKIE = "gx_session"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    id: str
    username: str
    display_name: str
    role: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=128)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role.value,
    )


@router.post("/auth/login", response_model=UserOut)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> UserOut:
    settings = get_settings()
    ip = _client_ip(request)
    try:
        check_rate_limit(ip or "unknown", body.username)
    except RateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    try:
        user = auth.authenticate(db, body.username, body.password)
    except auth.InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误"
        ) from exc
    except auth.AccountDisabledError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已停用") from exc
    reset_rate_limit(ip or "unknown", body.username)
    token, _ = auth.create_session(
        db,
        user,
        user_agent=request.headers.get("user-agent", ""),
        ip_address=ip,
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    # CSRF 双提交令牌 Cookie（非 HttpOnly，供前端读取放入 header）
    response.set_cookie(**csrf_cookie_options(max_age=settings.session_ttl_seconds))
    record_audit(
        db,
        action=AuditAction.login,
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        ip_address=ip,
    )
    return _user_out(user)


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        auth.revoke_session(db, token)
        record_audit(
            db,
            action=AuditAction.logout,
            target_type="user",
            ip_address=_client_ip(request),
        )
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


@router.get("/auth/csrf", status_code=status.HTTP_204_NO_CONTENT)
def csrf_token(response: Response) -> None:
    """下发新的 CSRF 双提交令牌 Cookie（整改 P0-A.11 兼容旧会话）。

    登录已下发 gx_csrf；旧会话/刷新页面后可调用本端点重新获取，
    前端据此恢复对状态变更请求的 CSRF 头。
    """
    settings = get_settings()
    response.set_cookie(**csrf_cookie_options(max_age=settings.session_ttl_seconds))


@router.get("/auth/me", response_model=UserOut)
def me(current_user: User = Depends(auth.get_current_user)) -> UserOut:
    return _user_out(current_user)


@router.post(
    "/auth/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> None:
    if not auth.verify_password(body.old_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="原密码错误")
    current_user.password_hash = auth.hash_password(body.new_password)
    db.commit()
    record_audit(
        db,
        action=AuditAction.password_change,
        actor_id=current_user.id,
        target_type="user",
        target_id=current_user.id,
        ip_address=_client_ip(request),
    )
