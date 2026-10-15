"""RAG 检索路由：对已入库手册进行授权检索。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gongxu.assets.indexing import retrieve_chunks
from gongxu.db.models import User, UserRole
from gongxu.db.session import get_db
from gongxu.security import auth

router = APIRouter(prefix="/rag", tags=["rag"])

READERS = (UserRole.admin, UserRole.instructor, UserRole.observer)


class RetrieveRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    asset_ids: list[str] = Field(default_factory=list)  # 显式资产白名单（整改 P0-3）
    top_k: int = Field(default=6, ge=1, le=20)


class ChunkOut(BaseModel):
    chunk_id: int
    asset_id: str
    chunk_index: int
    content_hash: str
    page: int | None = None
    section: str | None = None
    text: str
    score: float


@router.post("/retrieve", response_model=list[ChunkOut])
def retrieve(
    body: RetrieveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.get_current_user),
) -> list[ChunkOut]:
    if current_user.role not in READERS:
        raise HTTPException(status_code=403, detail="权限不足")
    if not body.asset_ids:
        raise HTTPException(status_code=400, detail="必须指定要检索的资产 asset_ids")
    return [
        ChunkOut(**c)
        for c in retrieve_chunks(db, body.query, asset_ids=body.asset_ids, top_k=body.top_k)
    ]
