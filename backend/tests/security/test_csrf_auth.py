"""安全测试：CSRF、登录限流、越权、审计。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gongxu.main import create_app
from gongxu.security.csrf import CSRF_COOKIE, CSRF_HEADER


@pytest.fixture(autouse=True)
def _enable_csrf(monkeypatch):
    """本模块显式开启 CSRF 验证。"""
    monkeypatch.setenv("GONGXU_CSRF_ENABLED", "true")
    from gongxu.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _login_with_csrf(client: TestClient, username: str, password: str):
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf, "登录应下发 CSRF Cookie"
    return csrf


def _user(db, *, username: str, password: str):
    from gongxu.db.models import UserRole
    from gongxu.security.auth import create_user

    create_user(db, username=username, password=password, role=UserRole.instructor)


def _inject_explicit_replay(monkeypatch) -> None:
    """Business-route tests opt into Replay without enabling it in production paths."""
    from gongxu.providers.replay import ReplayProvider

    monkeypatch.setattr("gongxu.api.routes.sops.get_provider", lambda: ReplayProvider())


def test_mutation_requires_csrf(db, video_asset, monkeypatch) -> None:
    """无 CSRF 头 → 状态变更被拒。"""
    _inject_explicit_replay(monkeypatch)
    _user(db, username="inst1", password="correct-horse-42")
    client = TestClient(create_app())
    csrf = _login_with_csrf(client, "inst1", "correct-horse-42")

    # 带 header 成功
    resp = client.post(
        "/api/v1/sops/compile",
        json={"process_key": "p", "reference_video_asset_id": video_asset, "manual_asset_ids": []},
        headers={CSRF_HEADER: csrf},
    )
    assert resp.status_code == 201

    # 无 header 被拒
    resp = client.post(
        "/api/v1/sops/compile",
        json={"process_key": "p", "reference_video_asset_id": video_asset, "manual_asset_ids": []},
    )
    assert resp.status_code == 403

    # 错误 header 被拒
    resp = client.post(
        "/api/v1/sops/compile",
        json={"process_key": "p", "reference_video_asset_id": video_asset, "manual_asset_ids": []},
        headers={CSRF_HEADER: "wrong-token"},
    )
    assert resp.status_code == 403


def test_login_rate_limit(db) -> None:
    """连续错误登录触发限流（429）。"""
    _user(db, username="inst1", password="correct-horse-42")
    client = TestClient(create_app())
    codes = []
    for _ in range(8):
        resp = client.post("/api/v1/auth/login", json={"username": "inst1", "password": "wrong"})
        codes.append(resp.status_code)
    assert codes[-1] == 429  # 超过 login_max_attempts=5


def test_learner_cannot_read_other_learner_session(db) -> None:
    from gongxu.db.models import UserRole
    from gongxu.security.auth import create_user

    create_user(db, username="stu_a", password="correct-horse-42", role=UserRole.learner)
    create_user(db, username="stu_b", password="correct-horse-42", role=UserRole.learner)
    from gongxu.assessment.service import create_session

    session = create_session(db, process_key="p", mode="manual_offline", learner_id=None)
    # stu_b 尝试读取无主会话
    client = TestClient(create_app())
    client.post("/api/v1/auth/login", json={"username": "stu_b", "password": "correct-horse-42"})
    resp = client.get(f"/api/v1/assessment-sessions/{session.id}")
    assert resp.status_code == 403


def test_learner_list_filtered(db) -> None:
    from gongxu.db.models import UserRole
    from gongxu.security.auth import create_user

    create_user(db, username="stu_a", password="correct-horse-42", role=UserRole.learner)
    from gongxu.assessment.service import create_session

    create_session(db, process_key="p", mode="manual_offline", learner_id="other-user-id")
    client = TestClient(create_app())
    client.post("/api/v1/auth/login", json={"username": "stu_a", "password": "correct-horse-42"})
    resp = client.get("/api/v1/assessment-sessions")
    assert resp.status_code == 200
    assert resp.json() == []  # learner 只看到自己的会话


def test_csrf_refresh_endpoint_restores_old_session(db, video_asset, monkeypatch) -> None:
    """旧会话（无 gx_csrf cookie）→ 调 /auth/csrf 刷新 → 写请求成功（前端自动恢复）。"""
    _inject_explicit_replay(monkeypatch)
    _user(db, username="inst1", password="correct-horse-42")
    client = TestClient(create_app())
    # 登录并取 session cookie；随后删除 csrf cookie 模拟旧会话
    resp = client.post(
        "/api/v1/auth/login", json={"username": "inst1", "password": "correct-horse-42"}
    )
    assert resp.status_code == 200
    client.cookies.delete(CSRF_COOKIE)
    assert client.cookies.get(CSRF_COOKIE) is None

    # 写请求无 csrf → 被拒
    resp = client.post(
        "/api/v1/sops/compile",
        json={"process_key": "p", "reference_video_asset_id": video_asset, "manual_asset_ids": []},
    )
    assert resp.status_code == 403

    # 前端逻辑：调 /auth/csrf 刷新令牌 → 带新 token 重试
    resp = client.get("/api/v1/auth/csrf")
    assert resp.status_code == 204
    new_csrf = client.cookies.get(CSRF_COOKIE)
    assert new_csrf, "刷新后应下发 CSRF Cookie"

    resp = client.post(
        "/api/v1/sops/compile",
        json={"process_key": "p", "reference_video_asset_id": video_asset, "manual_asset_ids": []},
        headers={CSRF_HEADER: new_csrf},
    )
    assert resp.status_code == 201
