# app/agent_push.py
from __future__ import annotations

import os
import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from .db import SessionLocal
from .models import Agent, AgentCheck

logger = logging.getLogger("agent")
router = APIRouter(prefix="/agent/push", tags=["agent_push"])

PUSH_SHARED_SECRET = (os.getenv("PUSH_SHARED_SECRET", "") or "").strip()

def _auth(req: Request) -> bool:
    if not PUSH_SHARED_SECRET:
        return False
    got = (req.headers.get("X-PUSH-SECRET") or "").strip()
    return got == PUSH_SHARED_SECRET

@router.post("/check")
async def push_check(req: Request):
    """
    Futuro: agente instalado no cliente chama este endpoint.
    Body esperado:
      { "instance": "...", "status": "online|degraded|offline", "latency_ms": 123, "error": "" }
    """
    if not _auth(req):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_json"}, status_code=400)

    instance = (body.get("instance") or "").strip()
    status = (body.get("status") or "unknown").strip().lower()
    latency_ms = body.get("latency_ms", None)
    error = (body.get("error") or "").strip() or None

    if not instance:
        return JSONResponse({"ok": False, "error": "missing_instance"}, status_code=400)

    with SessionLocal() as db:
        agent = db.execute(select(Agent).where(Agent.instance == instance).limit(1)).scalar_one_or_none()
        if not agent:
            return JSONResponse({"ok": False, "error": "unknown_instance"}, status_code=404)

        row = AgentCheck(
            client_id=agent.client_id,
            agent_id=agent.id,
            instance=agent.instance,
            mode="push",
            status=status,
            latency_ms=latency_ms if isinstance(latency_ms, int) else None,
            error=error,
        )
        db.add(row)
        db.commit()

    return JSONResponse({"ok": True})
