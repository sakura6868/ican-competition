"""API 路由汇总。"""

from __future__ import annotations

from fastapi import APIRouter

from gongxu.api.routes import (
    admin_users,
    assessment,
    assets,
    audit,
    auth,
    capabilities,
    demo,
    health,
    jobs,
    models,
    practice,
    rag,
    sops,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(admin_users.router)
api_router.include_router(audit.router)
api_router.include_router(jobs.router)
api_router.include_router(sops.router)
api_router.include_router(assets.router)
api_router.include_router(rag.router)
api_router.include_router(assessment.router)
api_router.include_router(models.router)
api_router.include_router(capabilities.router)
api_router.include_router(demo.router)
api_router.include_router(practice.router)
