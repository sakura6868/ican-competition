"""文档切片入库与 RAG 检索测试。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from gongxu.assets import service
from gongxu.assets.indexing import (
    ChunkError,
    get_chunk_provenance,
    index_document_asset,
    retrieve_chunks,
)
from gongxu.config import get_settings
from gongxu.db.models import DocumentChunk, UserRole
from gongxu.main import create_app
from gongxu.security.auth import create_user


def _register_manual(db, text: str) -> str:
    tmp = Path(get_settings().data_root) / "runtime" / "manual.txt"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    asset = service.register_asset(
        db, kind="manual", filename="manual.txt", temp_path=tmp, license_note="test"
    )
    return asset.id


LONG_MANUAL = (
    "安全操作规程：\n"
    + "步骤一，断电。拆卸角磨机前必须拔掉电源插头，确认上级断路器断开。\n" * 30
    + "步骤二，等待砂轮停转。确认砂轮完全停止转动后方可继续。\n" * 30
    + "步骤三，佩戴 PPE。双手戴防护手套，佩戴护目镜。\n" * 30
    + "拆卸使用一字螺丝刀与扳手，取下防护罩与锁紧螺母。\n" * 30
)


def test_index_document_asset(db) -> None:
    asset_id = _register_manual(db, LONG_MANUAL)
    result = index_document_asset(
        db, asset_id, work_dir=Path(get_settings().data_root) / "runtime" / "w"
    )
    assert result["chunks"] >= 4  # 长文本切多块
    assert result["chars"] > 0
    stored = db.query(DocumentChunk).filter(DocumentChunk.asset_id == asset_id).count()
    assert stored == result["chunks"]


def test_index_idempotent(db) -> None:
    asset_id = _register_manual(db, LONG_MANUAL)
    work = Path(get_settings().data_root) / "runtime" / "w2"
    first = index_document_asset(db, asset_id, work_dir=work)
    second = index_document_asset(db, asset_id, work_dir=work)
    assert first["chunks"] == second["chunks"]
    assert (
        db.query(DocumentChunk).filter(DocumentChunk.asset_id == asset_id).count()
        == second["chunks"]
    )


def test_index_rejects_video(db) -> None:
    tmp = Path(get_settings().data_root) / "runtime" / "v.mp4"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(b"\x00" * 100)
    asset = service.register_asset(
        db, kind="video", filename="v.mp4", temp_path=tmp, license_note=""
    )
    try:
        index_document_asset(db, asset.id, work_dir=tmp.parent)
        raise AssertionError("应拒绝视频资产")
    except ChunkError:
        pass


def test_retrieve_relevant_chunk(db) -> None:
    asset_id = _register_manual(db, LONG_MANUAL)
    index_document_asset(db, asset_id, work_dir=Path(get_settings().data_root) / "runtime" / "w3")
    hits = retrieve_chunks(db, "断电 拔掉电源插头", asset_ids=[asset_id], top_k=3)
    assert hits
    assert hits[0]["score"] >= 0
    assert "断电" in hits[0]["text"] or "插头" in hits[0]["text"]
    # 结果应包含 content_hash（整改 P0-3）
    assert hits[0]["content_hash"]


def test_markdown_retrieval_preserves_real_section_provenance(db) -> None:
    tmp = Path(get_settings().data_root) / "runtime" / "safety-manual.md"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(
        "# 准备\n检查工具。\n\n## 断电操作\n拔掉电源插头并等待停转。",
        encoding="utf-8",
    )
    asset = service.register_asset(
        db,
        kind="manual",
        filename=tmp.name,
        temp_path=tmp,
        license_note="test",
    )
    index_document_asset(db, asset.id, work_dir=tmp.parent / "markdown-index")

    hits = retrieve_chunks(db, "拔掉电源插头", asset_ids=[asset.id], top_k=3)
    hit = next(item for item in hits if "拔掉" in item["text"])
    assert hit["page"] is None
    assert hit["section"] == "断电操作"
    assert get_chunk_provenance(db, hit["chunk_id"]) == {
        "chunk_id": hit["chunk_id"],
        "asset_id": asset.id,
        "chunk_index": hit["chunk_index"],
        "content_hash": hit["content_hash"],
        "page": None,
        "section": "断电操作",
    }


def test_retrieve_requires_asset_ids(db) -> None:
    """空资产集合绝不检索全库（整改 P0-3）。"""
    asset_id = _register_manual(db, LONG_MANUAL)
    index_document_asset(db, asset_id, work_dir=Path(get_settings().data_root) / "runtime" / "w3b")
    assert retrieve_chunks(db, "断电") == []  # 无 asset_ids → 不检索
    assert retrieve_chunks(db, "断电", asset_ids=[]) == []
    # 未包含的资产集合 → 不返回该资产内容
    other = _register_manual(db, "完全不同的内容" * 50)
    index_document_asset(db, other, work_dir=Path(get_settings().data_root) / "runtime" / "w3c")
    hits = retrieve_chunks(db, "断电 插头", asset_ids=[other], top_k=3)
    assert all(h["asset_id"] == other for h in hits)


def test_retrieve_isolates_assets(db) -> None:
    """A/B 两份手册只选 A 时，B 的内容绝不返回（整改 P0-3 验收）。"""
    a = _register_manual(db, "暗号ALPHA " + "断电拔插头" * 40)
    b = _register_manual(db, "暗号BRAVO " + "等待停转" * 40)
    index_document_asset(db, a, work_dir=Path(get_settings().data_root) / "runtime" / "wa")
    index_document_asset(db, b, work_dir=Path(get_settings().data_root) / "runtime" / "wb")
    hits = retrieve_chunks(db, "等待停转", asset_ids=[a], top_k=6)
    joined = "".join(h["text"] for h in hits)
    assert "BRAVO" not in joined  # B 的暗号绝不出现在 A 的检索结果
    hits_b = retrieve_chunks(db, "等待停转", asset_ids=[b], top_k=6)
    assert hits_b  # B 自己检索有内容


def test_retrieve_empty_when_no_chunks(db) -> None:
    assert retrieve_chunks(db, "任意查询", asset_ids=["missing-asset"]) == []


def test_api_retrieve_requires_auth(db) -> None:
    client = TestClient(create_app())
    resp = client.post("/api/v1/rag/retrieve", json={"query": "断电", "top_k": 3})
    assert resp.status_code == 401  # 未登录拒绝


def test_api_retrieve_instructor(db) -> None:
    create_user(db, username="inst1", password="correct-horse-42", role=UserRole.instructor)
    asset_id = _register_manual(db, LONG_MANUAL)
    index_document_asset(db, asset_id, work_dir=Path(get_settings().data_root) / "runtime" / "w4")
    client = TestClient(create_app())
    client.post("/api/v1/auth/login", json={"username": "inst1", "password": "correct-horse-42"})
    resp = client.post(
        "/api/v1/rag/retrieve", json={"query": "断电 插头", "asset_ids": [asset_id], "top_k": 3}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) >= 1
    # 无 asset_ids → 400
    resp = client.post("/api/v1/rag/retrieve", json={"query": "断电", "top_k": 3})
    assert resp.status_code == 400
    assert {"chunk_id", "asset_id", "chunk_index", "text", "score"} <= set(body[0].keys())
