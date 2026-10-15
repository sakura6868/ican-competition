"""健康检查：供 Windows 冷启动、Docker 与探活使用。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from gongxu.config import get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(status="ok", app=settings.app_name, version="0.1.0")
