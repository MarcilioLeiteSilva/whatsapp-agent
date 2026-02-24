import os
import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .db import ensure_schema
from .admin_web import router as admin_web_router
from .portal_web import router as portal_router
from .monitoring import monitor_loop

logger = logging.getLogger("agent")

_monitor_task: asyncio.Task | None = None


def create_app() -> FastAPI:
    app = FastAPI(title="WhatsApp Agent SaaS")

    # CORS (ajuste se precisar)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers SSR
    app.include_router(admin_web_router)
    app.include_router(portal_router)

    @app.get("/status")
    async def status():
        return {"ok": True}

    @app.on_event("startup")
    async def _startup():
        global _monitor_task
        logger.info("APP_STARTUP_BEGIN")

        # garante schema mínimo
        ensure_schema()
        logger.info("DB_SCHEMA_READY")

        # inicia monitor loop
        _monitor_task = asyncio.create_task(monitor_loop())
        logger.info("MONITOR_LOOP_STARTED")

        logger.info("APP_STARTUP_DONE")

    @app.on_event("shutdown")
    async def _shutdown():
        global _monitor_task
        logger.info("APP_SHUTDOWN_BEGIN")

        if _monitor_task:
            _monitor_task.cancel()
            try:
                await _monitor_task
            except asyncio.CancelledError:
                pass
            _monitor_task = None

        logger.info("APP_SHUTDOWN_DONE")

    return app


app = create_app()
