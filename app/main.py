"""
app/main.py

Bootstrap principal da aplicação SaaS WhatsApp Agent
- Inicializa FastAPI
- Cria tabelas (MVP)
- Sobe monitor background loop
- Registra routers
"""

from __future__ import annotations

import os
import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .db import engine
from .models import Base
from .admin_web import router as admin_web_router
from .monitoring import monitor_loop

# Se você tiver portal_web separado:
try:
    from .portal_web import router as portal_web_router
    HAS_PORTAL = True
except Exception:
    HAS_PORTAL = False


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger("agent")


# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------
app = FastAPI(
    title="WhatsApp Agent SaaS",
    version="1.0.0",
)


# -----------------------------------------------------------------------------
# CORS (opcional)
# -----------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # depois pode restringir
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------------------------------------------------------
# Routers
# -----------------------------------------------------------------------------
app.include_router(admin_web_router)

if HAS_PORTAL:
    app.include_router(portal_web_router)


# -----------------------------------------------------------------------------
# Monitor Background Task
# -----------------------------------------------------------------------------
_monitor_task: asyncio.Task | None = None


@app.on_event("startup")
async def on_startup():
    """
    Inicializa:
    - Cria tabelas (MVP)
    - Sobe monitor loop
    """
    logger.info("APP_STARTUP_BEGIN")

    # cria tabelas automaticamente (MVP)
    Base.metadata.create_all(bind=engine)
    logger.info("DB_SCHEMA_READY")

    # sobe monitor background
    global _monitor_task
    try:
        _monitor_task = asyncio.create_task(monitor_loop())
        logger.info("MONITOR_LOOP_STARTED")
    except Exception as e:
        logger.exception("MONITOR_START_ERROR: %s", e)

    logger.info("APP_STARTUP_DONE")


@app.on_event("shutdown")
async def on_shutdown():
    """
    Cancela monitor background
    """
    logger.info("APP_SHUTDOWN_BEGIN")

    global _monitor_task
    if _monitor_task:
        _monitor_task.cancel()
        _monitor_task = None
        logger.info("MONITOR_LOOP_CANCELLED")

    logger.info("APP_SHUTDOWN_DONE")


# -----------------------------------------------------------------------------
# Health Endpoint
# -----------------------------------------------------------------------------
@app.get("/status")
async def status():
    """
    Health check simples
    """
    return JSONResponse(
        {
            "ok": True,
            "service": "whatsapp-agent-saas",
            "monitor_running": bool(_monitor_task),
        }
    )
    
