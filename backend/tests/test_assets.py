"""资产上传与文档解析测试。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gongxu.assets.parse import ParseError, extract_text
from gongxu.config import get_settings
from gongxu.db.models import UserRole
from gongxu.main import create_app
from gongxu.security.auth import create_user


def _make_user(db, *, role: UserRole, username: str):
    return create_user(db, username=username, password="correct-horse-42", role=role)


def _login(db, username: str) -> TestClient:
    client = TestClient(create_app())
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": "correct-horse-42"}
    )
    assert resp.status_code == 200
    return client


def test_upload_manual_asset(db) -> None:
    _make_user(db, role=UserRole.instructor, username="inst1")
    client = _login(db, "inst1")
    resp = client.post(
        "/api/v1/assets",
        files={"file": ("手册.txt", io.BytesIO("安全规范：先断电".encode()), "text/plain")},
        data={"kind": "manual", "license_note": "授权使用"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "manual"
    assert body["sha256"]
    assert body["has_encrypted_storage"] is True  # 密文已落盘
    # API 不暴露服务器内部路径（整改 R3.8）
    assert "encrypted_path" not in body


def test_upload_video_asset(db) -> None:
    _make_user(db, role=UserRole.instructor, username="inst1")
    client = _login(db, "inst1")
    resp = client.post(
        "/api/v1/assets",
        files={"file": ("demo.mp4", io.BytesIO(b"\x00" * 2000), "video/mp4")},
        data={"kind": "video", "license_note": "公司授权"},
    )
    assert resp.status_code == 201
    assert resp.json()["kind"] == "video"


def test_learner_cannot_upload_manual(db) -> None:
    _make_user(db, role=UserRole.learner, username="stu1")
    client = _login(db, "stu1")
    resp = client.post(
        "/api/v1/assets",
        files={"file": ("a.txt", io.BytesIO(b"x"), "text/plain")},
        data={"kind": "manual"},
    )
    assert resp.status_code == 403


def test_unsupported_kind_rejected(db) -> None:
    _make_user(db, role=UserRole.instructor, username="inst1")
    client = _login(db, "inst1")
    resp = client.post(
        "/api/v1/assets",
        files={"file": ("a.txt", io.BytesIO(b"x"), "text/plain")},
        data={"kind": "audio"},
    )
    assert resp.status_code == 400


def test_list_assets_requires_reader(db) -> None:
    _make_user(db, role=UserRole.instructor, username="inst1")
    client = _login(db, "inst1")
    resp = client.post(
        "/api/v1/assets",
        files={"file": ("手册.md", io.BytesIO("# 规范".encode()), "text/markdown")},
        data={"kind": "manual"},
    )
    assert resp.status_code == 201
    listing = client.get("/api/v1/assets")
    assert listing.status_code == 200
    assert len(listing.json()) == 1


def test_extract_text_txt(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("安全操作规程", encoding="utf-8")
    assert "安全操作规程" in extract_text(f)


def test_extract_text_unsupported(tmp_path: Path) -> None:
    f = tmp_path / "a.xyz"
    f.write_bytes(b"data")
    with pytest.raises(ParseError, match="不支持"):
        extract_text(f)


def test_learner_can_upload_and_preview_only_own_video(db) -> None:
    _make_user(db, role=UserRole.learner, username="stu1")
    _make_user(db, role=UserRole.learner, username="stu2")
    first = _login(db, "stu1")
    second = _login(db, "stu2")

    uploaded = first.post(
        "/api/v1/assets",
        files={"file": ("attempt.mp4", io.BytesIO(b"0123456789"), "video/mp4")},
        data={"kind": "video"},
    )
    assert uploaded.status_code == 201, uploaded.text
    asset_id = uploaded.json()["id"]
    listing = first.get("/api/v1/assets")
    assert [item["id"] for item in listing.json()] == [asset_id]

    ranged = first.get(
        f"/api/v1/assets/{asset_id}/preview",
        headers={"Range": "bytes=2-5"},
    )
    assert ranged.status_code == 206
    assert ranged.content == b"2345"
    assert ranged.headers["content-range"] == "bytes 2-5/10"
    assert second.get(f"/api/v1/assets/{asset_id}/preview").status_code == 403

    preview_root = get_settings().data_root / "runtime" / "previews"
    assert not preview_root.exists() or not any(preview_root.iterdir())


def test_upload_and_process_returns_job_for_learner_video(db) -> None:
    _make_user(db, role=UserRole.learner, username="stu1")
    client = _login(db, "stu1")
    response = client.post(
        "/api/v1/assets/upload-and-process",
        files={"file": ("attempt.mp4", io.BytesIO(b"video"), "video/mp4")},
        data={"kind": "video"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["asset"]["kind"] == "video"
    assert response.json()["job_status"] == "queued"
    assert client.get(f"/api/v1/jobs/{response.json()['job_id']}").status_code == 200


def test_process_failed_asset_requeues_same_job_instead_of_returning_terminal(db) -> None:
    from gongxu.db.models import Job, JobStatus

    _make_user(db, role=UserRole.instructor, username="asset_retry")
    client = _login(db, "asset_retry")
    uploaded = client.post(
        "/api/v1/assets/upload-and-process",
        files={"file": ("manual.txt", io.BytesIO("断电".encode()), "text/plain")},
        data={"kind": "manual", "license_note": "测试授权"},
    )
    assert uploaded.status_code == 201
    asset_id = uploaded.json()["asset"]["id"]
    job_id = uploaded.json()["job_id"]
    job = db.get(Job, job_id)
    assert job is not None
    job.status = JobStatus.failed
    job.retry_count = job.max_retries + 1
    job.error_summary = "测试终态失败"
    db.commit()

    retried = client.post(f"/api/v1/assets/{asset_id}/process")
    assert retried.status_code == 200
    assert retried.json() == {"job_id": job_id, "status": "queued"}
    db.refresh(job)
    assert job.retry_count == 0
    assert job.error_summary is None


def test_manual_becomes_indexed_only_after_processing_job(db, test_db) -> None:
    _make_user(db, role=UserRole.instructor, username="indexer")
    client = _login(db, "indexer")
    response = client.post(
        "/api/v1/assets/upload-and-process",
        files={
            "file": (
                "procedure.txt",
                io.BytesIO("断电后拆卸外壳，确认工具和工件可见。".encode()),
                "text/plain",
            )
        },
        data={"kind": "manual", "license_note": "测试授权"},
    )
    assert response.status_code == 201, response.text
    asset_id = response.json()["asset"]["id"]

    queued = {item["id"]: item for item in client.get("/api/v1/assets").json()}[asset_id]
    assert queued["indexed"] is False
    assert queued["processing_status"] == "queued"

    from gongxu.worker import _claim_and_run

    assert _claim_and_run(test_db) is True
    indexed = {item["id"]: item for item in client.get("/api/v1/assets").json()}[asset_id]
    assert indexed["indexed"] is True
    assert indexed["processing_status"] == "succeeded"
