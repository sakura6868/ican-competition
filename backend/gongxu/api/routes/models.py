"""模型登记路由：查询本地模型可用状态（handoff §7 门禁）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from gongxu.db.models import User
from gongxu.db.session import get_db
from gongxu.security import auth
from gongxu.vision import models as model_service

router = APIRouter(prefix="/models", tags=["models"])


class ModelOut(BaseModel):
    id: str
    name: str
    kind: str
    status: str
    format: str
    version: str
    sha256: str
    latency_ms: int
    notes: str


@router.get("/info", response_model=list[ModelOut])
def model_info(
    db: Session = Depends(get_db),
    _user: User = Depends(auth.get_current_user),
) -> list[ModelOut]:
    model_service.ensure_required_registered(db)
    return [
        ModelOut(
            id=m.id,
            name=m.name,
            kind=m.kind,
            status=m.status.value,
            format=m.format,
            version=m.version,
            sha256=m.sha256,
            latency_ms=m.latency_ms,
            notes=m.notes,
        )
        for m in model_service.list_models(db)
    ]
