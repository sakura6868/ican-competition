"""认证与角色权限测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from gongxu.db.models import UserRole
from gongxu.main import create_app
from gongxu.security.auth import create_user


def _make_user(db, *, role: UserRole = UserRole.learner, username: str = "learner1"):
    return create_user(
        db, username=username, password="correct-horse-42", role=role, display_name="测试用户"
    )


def _client(db) -> TestClient:
    return TestClient(create_app())


def test_login_me_logout_flow(db) -> None:
    _make_user(db)
    client = _client(db)

    # 未登录访问 /me 被拒绝
    assert client.get("/api/v1/auth/me").status_code == 401

    # 登录成功，返回用户信息并设置 HttpOnly Cookie
    resp = client.post(
        "/api/v1/auth/login", json={"username": "learner1", "password": "correct-horse-42"}
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == "learner1"
    assert "gx_session" in resp.cookies

    # 带 Cookie 访问 /me
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["role"] == "learner"

    # 登出后 /me 被拒绝
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401


def test_login_wrong_password(db) -> None:
    _make_user(db)
    client = _client(db)
    resp = client.post("/api/v1/auth/login", json={"username": "learner1", "password": "wrong"})
    assert resp.status_code == 401


def test_login_unknown_user_also_401(db) -> None:
    client = _client(db)
    resp = client.post("/api/v1/auth/login", json={"username": "nobody", "password": "whatever-1"})
    assert resp.status_code == 401


def test_change_password(db) -> None:
    user = _make_user(db)
    client = _client(db)
    client.post("/api/v1/auth/login", json={"username": "learner1", "password": "correct-horse-42"})

    resp = client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "correct-horse-42", "new_password": "brand-new-pass-99"},
    )
    assert resp.status_code == 204

    # 旧密码失效，新密码可登录
    client.post("/api/v1/auth/logout")
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "learner1", "password": "correct-horse-42"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "learner1", "password": "brand-new-pass-99"}
        ).status_code
        == 200
    )


def test_learner_cannot_admin_users(db) -> None:
    _make_user(db, role=UserRole.learner)
    client = _client(db)
    client.post("/api/v1/auth/login", json={"username": "learner1", "password": "correct-horse-42"})
    assert client.get("/api/v1/admin/users").status_code == 403


def test_admin_can_create_and_list_users(db) -> None:
    _make_user(db, role=UserRole.admin, username="admin1")
    client = _client(db)
    client.post("/api/v1/auth/login", json={"username": "admin1", "password": "correct-horse-42"})

    resp = client.post(
        "/api/v1/admin/users",
        json={
            "username": "trainee",
            "password": "pass-123456",
            "role": "learner",
            "display_name": "新学员",
        },
    )
    assert resp.status_code == 201
    created_id = resp.json()["id"]

    listing = client.get("/api/v1/admin/users")
    assert listing.status_code == 200
    usernames = {u["username"] for u in listing.json()}
    assert {"admin1", "trainee"} <= usernames

    patch = client.patch(f"/api/v1/admin/users/{created_id}", json={"is_active": False})
    assert patch.status_code == 200
    assert patch.json()["is_active"] is False


def test_duplicate_username_rejected(db) -> None:
    _make_user(db, role=UserRole.admin, username="admin1")
    client = _client(db)
    client.post("/api/v1/auth/login", json={"username": "admin1", "password": "correct-horse-42"})
    resp = client.post(
        "/api/v1/admin/users",
        json={"username": "admin1", "password": "pass-123456", "role": "observer"},
    )
    assert resp.status_code == 400
