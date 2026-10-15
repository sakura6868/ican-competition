"""能力清单必须反映实际运行模式，不能把未配置能力写成已集成。"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from gongxu.config import get_settings
from gongxu.db.models import AuditAction, AuditLog, UserRole
from gongxu.main import create_app
from gongxu.security.auth import create_user


def _logged_in_client(db) -> TestClient:
    create_user(
        db,
        username="cap-user",
        password="correct-horse-42",
        role=UserRole.instructor,
        display_name="能力测试",
    )
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "cap-user", "password": "correct-horse-42"},
    )
    assert response.status_code == 200
    return client


def test_capabilities_fail_closed_without_vision_backend(db) -> None:
    client = _logged_in_client(db)
    response = client.get("/api/v1/system/capabilities")
    assert response.status_code == 200
    capabilities = {item["key"]: item for item in response.json()}

    assert capabilities["recorded_assessment"]["status"] == "implemented"
    assert "可信拒判" in capabilities["recorded_assessment"]["note"]
    assert capabilities["recorded_assessment"]["runtime_backend"] == "none"
    assert capabilities["recorded_assessment"]["provider_export_required"] is False
    assert "live_camera" not in capabilities


def test_removed_workstation_routes_are_not_exposed(db) -> None:
    client = _logged_in_client(db)

    assert client.get("/api/v1/workstations").status_code == 404


def test_capabilities_show_configured_qwen_mode(db, monkeypatch) -> None:
    monkeypatch.setenv("GONGXU_LLM_MODE", "live")
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-provider-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "test-vision-model")
    monkeypatch.setenv("GONGXU_VISION_BACKEND", "qwen")
    get_settings.cache_clear()
    client = _logged_in_client(db)

    response = client.get("/api/v1/system/capabilities")
    assert response.status_code == 200
    capabilities = {item["key"]: item for item in response.json()}

    assert capabilities["video_to_sop"]["status"] == "integrated"
    assert capabilities["recorded_assessment"]["status"] == "integrated"
    assert capabilities["recorded_assessment"]["runtime_backend"] == "qwen"
    assert capabilities["recorded_assessment"]["provider_export_required"] is True
    assert "test-vision-model" in capabilities["vision"]["note"]
    assert all(item["status"] != "validated" for item in capabilities.values())


def test_generic_onnx_does_not_claim_integrated_industrial_assessment(
    db, monkeypatch
) -> None:
    monkeypatch.setenv("GONGXU_VISION_BACKEND", "onnx")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "gongxu.api.routes.capabilities.model_service.list_models",
        lambda _db: [
            SimpleNamespace(name="hand", status=SimpleNamespace(value="available")),
            SimpleNamespace(name="tool_detector", status=SimpleNamespace(value="available")),
        ],
    )
    client = _logged_in_client(db)

    response = client.get("/api/v1/system/capabilities")
    assert response.status_code == 200
    capabilities = {item["key"]: item for item in response.json()}

    assert capabilities["recorded_assessment"]["status"] == "implemented"
    assert capabilities["vision"]["status"] == "implemented"
    assert "仅手部/通用对象事实" in capabilities["recorded_assessment"]["note"]


def test_provider_status_does_not_make_network_call(db, monkeypatch) -> None:
    monkeypatch.setenv("GONGXU_LLM_MODE", "live")
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-provider-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen-test")
    monkeypatch.setenv("GONGXU_VISION_BACKEND", "qwen")
    get_settings.cache_clear()
    client = _logged_in_client(db)

    response = client.get("/api/v1/system/provider-status")

    assert response.status_code == 200
    assert response.json() == {
        "provider": "qwen",
        "configured": True,
        "runtime_backend": "qwen",
        "mode": "live",
        "model_id": "qwen-test",
        "vision_ok": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "note": "Qwen 已配置为主视觉模型；真人视频仅在逐资产授权后发送压缩关键帧",
    }
    get_settings.cache_clear()


def test_provider_probe_uses_synthetic_image_contract_and_audits(db, monkeypatch) -> None:
    monkeypatch.setenv("GONGXU_LLM_MODE", "live")
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-provider-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen-test")
    monkeypatch.setenv("GONGXU_VISION_BACKEND", "qwen")
    get_settings.cache_clear()

    class _Provider:
        def probe(self):
            return {
                "ok": True,
                "vision_ok": True,
                "model_id": "qwen-test",
                "usage": {"input_tokens": 31, "output_tokens": 7, "total_tokens": 38},
            }

    monkeypatch.setattr("gongxu.providers.factory.get_provider", lambda: _Provider())
    client = _logged_in_client(db)

    response = client.post("/api/v1/system/provider-probe")

    assert response.status_code == 200
    body = response.json()
    assert body["vision_ok"] is True
    assert body["total_tokens"] == 38
    audit = (
        db.query(AuditLog)
        .filter(AuditLog.action == AuditAction.provider_call)
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit is not None
    assert audit.target_type == "provider_probe"
    assert audit.detail["synthetic_image_only"] is True
    get_settings.cache_clear()
