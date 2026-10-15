"""审计路由测试：记录、列表与权限。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from gongxu.db.models import AuditAction, UserRole
from gongxu.main import create_app
from gongxu.security.audit import record_audit
from gongxu.security.auth import create_user


def _make_user(db, *, role: UserRole = UserRole.learner, username: str = "learner1"):
    return create_user(
        db, username=username, password="correct-horse-42", role=role, display_name="测试用户"
    )


def _client(db) -> TestClient:
    return TestClient(create_app())


def _login(client: TestClient, username: str) -> None:
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "correct-horse-42"}
    )
    assert resp.status_code == 200


def test_audit_recorded_on_login(db) -> None:
    _make_user(db)
    client = _client(db)
    _login(client, "learner1")
    resp = client.get("/api/v1/audit")
    assert resp.status_code == 403  # learner 无权查看审计


def test_admin_can_list_audit(db) -> None:
    admin = _make_user(db, role=UserRole.admin, username="admin1")
    db.commit()
    record_audit(
        db,
        action=AuditAction.login,
        actor_id=admin.id,
        target_type="user",
        target_id=admin.id,
        detail={"note": "manual"},
        ip_address="127.0.0.1",
    )
    client = _client(db)
    _login(client, "admin1")
    resp = client.get("/api/v1/audit")
    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) >= 1
    assert any(e["action"] == "login" for e in entries)

    summary = client.get("/api/v1/audit/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["total"] >= 1
    assert "login" in body["by_action"]


def test_observer_can_read_audit(db) -> None:
    _make_user(db, role=UserRole.observer, username="obs1")
    client = _client(db)
    _login(client, "obs1")
    resp = client.get("/api/v1/audit")
    assert resp.status_code == 200
