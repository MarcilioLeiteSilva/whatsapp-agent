# app/monitoring.py
from __future__ import annotations

import os
import time
import asyncio
import secrets
import logging
from typing import Optional

import httpx
from sqlalchemy import select, desc, delete

from .db import SessionLocal
from .models import Agent, AgentCheck

logger = logging.getLogger("agent")


MONITOR_ENABLED = os.getenv("MONITOR_ENABLED", "true").strip().lower() in ("1", "true", "yes", "y")
MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "20"))
MONITOR_TIMEOUT_SECONDS = float(os.getenv("MONITOR_TIMEOUT_SECONDS", "5"))
MONITOR_DEGRADED_MS = int(os.getenv("MONITOR_DEGRADED_MS", "1200"))

# endpoint “genérico” (funciona com qualquer base_url)
# se você quiser testar uma rota real da Evolution depois, só muda via env
MONITOR_PING_PATH = (os.getenv("MONITOR_PING_PATH", "/") or "/").strip()
MONITOR_APIKEY_HEADER = (os.getenv("MONITOR_APIKEY_HEADER", "apikey") or "apikey").strip()

# retenção
MONITOR_KEEP_PER_AGENT = int(os.getenv("MONITOR_KEEP_PER_AGENT", "50"))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _normalize_base_url(s: Optional[str]) -> str:
    return (s or "").strip().rstrip("/")


def _build_ping_url(base_url: str) -> str:
    base_url = _normalize_base_url(base_url)
    path = MONITOR_PING_PATH
    if not path.startswith("/"):
        path = "/" + path
    return f"{base_url}{path}"



#Inicio check one

async def _check_one(agent: Agent) -> dict:
    """
    Retorna dict com status/latency/error/details
    """
    base_url = _normalize_base_url(getattr(agent, "evolution_base_url", None))
    api_key = (getattr(agent, "api_key", None) or "").strip()

    if not base_url:
        return {
            "status": "unknown",
            "latency_ms": None,
            "error": "missing_base_url",
            "details": {"reason": "agent.evolution_base_url vazio"},
        }

    url = _build_ping_url(base_url)
    headers = {}
    if api_key:
        headers[MONITOR_APIKEY_HEADER] = api_key

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=MONITOR_TIMEOUT_SECONDS) as c:
            r = await c.get(url, headers=headers)
        ms = int(round((time.time() - t0) * 1000))

        if 200 <= r.status_code < 400:
            status = "degraded" if ms >= MONITOR_DEGRADED_MS else "online"
            return {
                "status": status,
                "latency_ms": ms,
                "error": None,
                "details": {"url": url, "http_status": r.status_code},
            }

        return {
            "status": "offline",
            "latency_ms": ms,
            "error": f"http_{r.status_code}",
            "details": {"url": url, "http_status": r.status_code},
        }

    except Exception as e:
        ms = int(round((time.time() - t0) * 1000))
        err = str(e)
        if len(err) > 300:
            err = err[:300] + "..."
        return {
            "status": "offline",
            "latency_ms": ms,
            "error": err,
            "details": {"url": url, "exception": str(type(e).__name__)},
        }


#Final check one

def _save_check(agent_id: str, result: dict) -> None:
    with SessionLocal() as db:
        row = AgentCheck(
            id=_new_id("chk"),
            agent_id=agent_id,
            status=result.get("status") or "unknown",
            latency_ms=result.get("latency_ms"),
            error=result.get("error"),
            details=result.get("details"),
        )
        db.add(row)
        db.commit()

        # retenção: mantém só os últimos N checks por agent
        if MONITOR_KEEP_PER_AGENT > 0:
            ids = (
                db.execute(
                    select(AgentCheck.id)
                    .where(AgentCheck.agent_id == agent_id)
                    .order_by(desc(AgentCheck.checked_at))
                    .offset(MONITOR_KEEP_PER_AGENT)
                )
                .scalars()
                .all()
            )
            if ids:
                db.execute(delete(AgentCheck).where(AgentCheck.id.in_(ids)))
                db.commit()


async def monitor_loop() -> None:
    if not MONITOR_ENABLED:
        logger.info("MONITOR_LOOP_DISABLED")
        return

    logger.info("MONITOR_LOOP_START interval=%ss timeout=%ss ping_path=%s", MONITOR_INTERVAL_SECONDS, MONITOR_TIMEOUT_SECONDS, MONITOR_PING_PATH)

    while True:
        try:
            with SessionLocal() as db:
                agents = db.execute(select(Agent).order_by(desc(Agent.created_at))).scalars().all()

            # faz checks em paralelo com limite simples
            sem = asyncio.Semaphore(10)

            async def run_one(a: Agent):
                async with sem:
                    res = await _check_one(a)
                    _save_check(str(a.id), res)

            await asyncio.gather(*(run_one(a) for a in agents))

        except Exception as e:
            logger.exception("MONITOR_LOOP_ERROR: %s", e)

        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)
