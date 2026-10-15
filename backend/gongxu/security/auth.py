"""认证服务：用户创建、登录、会话管理与权限依赖。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.db.models import AuthSession, User, UserRole, utcnow
from gongxu.db.session import get_db
from gongxu.security.tokens import (
    generate_token,
    hash_token,
)
from gongxu.security.tokens import (
    hash_password as hash_password,
)
from gongxu.security.tokens import (
    verify_password as verify_password,
)


class AuthError(Exception):
    """认证失败基类。"""


class UserNotFoundError(AuthError):
    pass


class InvalidCredentialsError(AuthError):
    pass


class AccountDisabledError(AuthError):
    pass


def create_user(
    db: Session,
    *,
    username: str,
    password: str,
    role: UserRole,
    display_name: str = "",
) -> User:
    settings = get_settings()
    if len(password) < settings.password_min_length:
        raise AuthError(f"密码长度不能少于 {settings.password_min_length} 位")
    existing = db.scalar(select(User).where(User.username == username))
    if existing is not None:
        raise AuthError("用户名已存在")
    user = User(
        username=username,
        display_name=display_name,
        password_hash=hash_password(password),
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: Session, username: str, password: str) -> User:
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        # 恒定时间返回，避免用户名枚举
        verify_password(password, _DUMMY_PASSWORD_HASH)
        raise InvalidCredentialsError("用户名或密码错误")
    if not verify_password(password, user.password_hash):
        raise InvalidCredentialsError("用户名或密码错误")
    if not user.is_active:
        raise AccountDisabledError("账号已停用")
    return user


_DUMMY_PASSWORD_HASH = (  # nosec B105：恒定时间占位哈希
    "$argon2id$v=19$m=65536,t=3,p=4$prM0wKWCoMLuK4rdJzxWAg$"
    "echs64NPhZtk5PqXOs2ridGon3MlOsr7zL+aJobL8S4"
)


def create_session(
    db: Session,
    user: User,
    *,
    user_agent: str = "",
    ip_address: str = "",
) -> tuple[str, AuthSession]:
    settings = get_settings()
    token = generate_token()
    session = AuthSession(
        user_id=user.id,
        token_hash=hash_token(token),
        expires_at=utcnow() + timedelta(seconds=settings.session_ttl_seconds),
        user_agent=user_agent,
        ip_address=ip_address,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return token, session


def _get_user_by_session(db: Session, token: str) -> User:
    digest = hash_token(token)
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest))
    if session is None:
        raise InvalidCredentialsError("会话不存在")
    now = utcnow()
    if session.expires_at < now:
        raise InvalidCredentialsError("会话已过期")
    if session.revoked_at is not None:
        raise InvalidCredentialsError("会话已撤销")
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise InvalidCredentialsError("账号不可用")
    return user


def revoke_session(db: Session, token: str) -> None:
    digest = hash_token(token)
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest))
    if session is not None and session.revoked_at is None:
        session.revoked_at = utcnow()
        db.commit()


def resolve_token_user(db: Session, token: str) -> User:
    """从令牌解析用户；失败时抛出 AuthError。"""
    if not token:
        raise InvalidCredentialsError("未登录")
    return _get_user_by_session(db, token)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """从 HttpOnly Cookie 或 Authorization 头读取会话令牌。"""
    token = None
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
    else:
        token = request.cookies.get("gx_session")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return _get_user_by_session(db, token)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_roles(*roles: UserRole) -> Callable[..., User]:
    """返回 FastAPI 依赖：仅允许指定角色访问。"""

    def _dependency(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足",
            )
        return current_user

    return _dependency
