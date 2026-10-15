"""健康检查测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from gongxu.main import create_app


def test_health_ok() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["app"] == "gongxu-zhichuan"
