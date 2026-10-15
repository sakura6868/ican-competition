"""文档切片入库与 RAG 检索对接（handoff §8）。

- 提取手册文本（PDF/DOCX/TXT）
- 按 500 字、80 重叠切片
- AES-256-GCM 加密每块入库；明文不入库
- 授权检索：在内存解密所有块 → HybridRetriever 混合排序
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gongxu.assets import service as asset_service
from gongxu.assets.parse import ParseError, extract_sections
from gongxu.db.models import DocumentChunk, SourceAsset
from gongxu.rag.retriever import HybridRetriever, split_text
from gongxu.security import crypto


class ChunkError(Exception):
    pass


_CHUNK_PAYLOAD_VERSION = 1


def _encode_chunk_payload(text: str, *, page: int | None, section: str | None) -> str:
    return json.dumps(
        {
            "_gx_chunk_version": _CHUNK_PAYLOAD_VERSION,
            "text": text,
            "page": page,
            "section": section,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _decode_chunk_payload(value: str) -> dict[str, Any]:
    """兼容旧库纯文本块；新块返回可验证的页码/章节。"""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"text": value, "page": None, "section": None}
    if not isinstance(parsed, dict) or parsed.get("_gx_chunk_version") != _CHUNK_PAYLOAD_VERSION:
        return {"text": value, "page": None, "section": None}
    return {
        "text": str(parsed.get("text") or ""),
        "page": parsed.get("page") if isinstance(parsed.get("page"), int) else None,
        "section": str(parsed["section"]) if parsed.get("section") else None,
    }


def get_chunk_provenance(db: Session, chunk_id: int) -> dict[str, Any] | None:
    row = db.get(DocumentChunk, chunk_id)
    if row is None:
        return None
    decoded = _decode_chunk_payload(crypto.decrypt_text(row.content_encrypted))
    return {
        "chunk_id": row.id,
        "asset_id": row.asset_id,
        "chunk_index": row.chunk_index,
        "content_hash": row.content_hash,
        "page": decoded["page"],
        "section": decoded["section"],
    }


def index_document_asset(
    db: Session,
    asset_id: str,
    *,
    work_dir: Path,
) -> dict[str, Any]:
    """解密手册 → 提取文本 → 切片 → 加密入库（幂等：先清空旧块）。"""
    asset = db.get(SourceAsset, asset_id)
    if asset is None:
        raise ChunkError(f"资产不存在: {asset_id}")
    if asset.kind == "video":
        raise ChunkError("视频资产不参与文档切片；使用 OCR/ASR 产物另行处理")

    plain = asset_service.decrypt_to_temp(db, asset_id, temp_dir=work_dir)
    try:
        sections = extract_sections(plain)
    except ParseError as exc:
        raise ChunkError(f"文档解析失败: {exc}") from exc

    chunks: list[tuple[str, int | None, str | None]] = []
    for parsed_section in sections:
        chunks.extend(
            (chunk, parsed_section.page, parsed_section.section)
            for chunk in split_text(parsed_section.text)
        )
    if not chunks:
        raise ChunkError("文档未提取到文本内容")

    # 幂等：重新入库前删除旧块
    old = db.scalars(select(DocumentChunk).where(DocumentChunk.asset_id == asset_id))
    for row in old:
        db.delete(row)
    db.flush()

    created = 0
    for index, (chunk, page, section_name) in enumerate(chunks):
        db.add(
            DocumentChunk(
                asset_id=asset_id,
                chunk_index=index,
                content_encrypted=crypto.encrypt_text(
                    _encode_chunk_payload(chunk, page=page, section=section_name)
                ),
                content_hash=hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
            )
        )
        created += 1
    db.commit()
    return {
        "asset_id": asset_id,
        "chunks": created,
        "chars": sum(len(chunk) for chunk, _page, _section in chunks),
    }


def retrieve_chunks(
    db: Session,
    query: str,
    *,
    asset_ids: list[str] | None = None,
    top_k: int = 6,
) -> list[dict[str, Any]]:
    """授权检索：仅在显式资产白名单内解密并混合排序（整改 P0-3）。

    asset_ids 为空/None 时返回空列表，绝不检索全库，防止把未授权文档
    片段外发给 Provider。
    """
    if not asset_ids:
        return []
    rows = db.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.asset_id.in_(asset_ids))
        .order_by(DocumentChunk.asset_id, DocumentChunk.chunk_index)
    ).all()
    if not rows:
        return []
    decrypted = [_decode_chunk_payload(crypto.decrypt_text(row.content_encrypted)) for row in rows]
    retriever = HybridRetriever([item["text"] for item in decrypted], top_k=top_k)
    hits = retriever.search(query)
    results = []
    seen_ids: set[int] = set()
    for hit in hits:
        row = rows[hit["index"]]
        locator = decrypted[hit["index"]]
        if row.id in seen_ids:
            continue  # 去重（整改 P0-3）
        seen_ids.add(row.id)
        results.append(
            {
                "chunk_id": row.id,
                "asset_id": row.asset_id,
                "chunk_index": row.chunk_index,
                "content_hash": row.content_hash,
                "page": locator["page"],
                "section": locator["section"],
                "text": hit["text"],
                "score": hit["score"],
            }
        )
    return results
