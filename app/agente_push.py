# app/agent_push.py
from __future__ import annotations

import os
import logging
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from .db import SessionLocal
from .models import Agent, AgentCheck

logger = logging.getLogger("agent")
router = APIRouter(prefix="/agent/push", tags=["agent_push"])

PUSH_SHARED_SECRET = (os.getenv("PUSH_SHARED_SECRET", "") or "").strip()


def _auth(req: Request) -> bool:
    """
    Auth simples por shared secret.
    Se não estiver configurado, nega por padrão (mais seguro).
    """
    if not PUSH_SHARED_SECRET:
        return False
    got = (req.headers.get("X-PUSH-SECRET") or "").strip()
    return got == PUSH_SHARED_SECRET


def _sanitize_status(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("online", "degraded", "offline", "unknown"):
        return s
    return "unknown"


@router.post("/check")
async def push_check(req: Request):
    """
    Agente instalado no cliente chama este endpoint para empurrar status.
    Body esperado (flexível):
      {
        "instance": "agente001",
        "status": "online|degraded|offline|unknown",
        "latency_ms": 123,
        "error": "..."
      }

    Persiste em agent_checks:
      - agent_id
      - status
      - latency_ms
      - error
      - details (guarda: mode=push, instance, client_id, ip, payload)
    """
    if not _auth(req):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_json"}, status_code=400)

    instance = (body.get("instance") or "").strip()
    status = _sanitize_status(body.get("status") or "unknown")

    latency_ms = body.get("latency_ms", None)
    latency_ms_int = latency_ms if isinstance(latency_ms, int) else None

    error = (body.get("error") or "").strip() or None

    if not instance:
        return JSONResponse({"ok": False, "error": "missing_instance"}, status_code=400)

    # detalhes úteis pra diagnóstico
    details: Dict[str, Any] = {
        "mode": "push",
        "instance": instance,
        "ip": req.client.host if req.client else None,
        "payload": body,
    }

    with SessionLocal() as db:
        agent = db.execute(
            select(Agent).where(Agent.instance == instance).limit(1)
        ).scalar_one_or_none()

        if not agent:
            return JSONResponse({"ok": False, "error": "unknown_instance"}, status_code=404)

        # adiciona infos do agente no details (sem mexer no schema)
        details["agent_id"] = str(agent.id)
        details["client_id"] = str(agent.client_id)

        row = AgentCheck(
            agent_id=str(agent.id),
            status=status,
            latency_ms=latency_ms_int,
            error=error,
            details=details,
        )
        db.add(row)
        db.commit()

    return JSONResponse({"ok": True})
