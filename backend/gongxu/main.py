"""FastAPI 应用工厂与生命周期。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from gongxu.api.router import api_router
from gongxu.config import get_settings
from gongxu.db.session import init_db
from gongxu.runtime_cleanup import cleanup_api_request_temps


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    cleanup_api_request_temps(settings)
    init_db(settings)
    task = None
    if settings.practice_worker_enabled:
        from gongxu.practice_worker import serve

        task = asyncio.create_task(serve())
    try:
        yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
