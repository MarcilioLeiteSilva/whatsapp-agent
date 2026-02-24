# app/monitoring.py
from __future__ import annotations

import os
import time
import asyncio
import logging
from typing import Optional, Any, Dict

import httpx
from sqlalchemy import select, desc, delete

from .db import SessionLocal
from .models import Agent, AgentCheck

logger = logging.getLogger("agent")

MONITOR_ENABLED = os.getenv("MONITOR_ENABLED", "true").strip().lower() in ("1", "true", "yes", "y")
MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "20"))
MONITOR_TIMEOUT_SECONDS = float(os.getenv("MONITOR_TIMEOUT_SECONDS", "5"))
MONITOR_DEGRADED_MS = int(os.getenv("MONITOR_DEGRADED_MS", "1200"))

# Retenção
MONITOR_KEEP_PER_AGENT = int(os.getenv("MONITOR_KEEP_PER_AGENT", "50"))

# Header padrão da Evolution (docs): apikey
MONITOR_APIKEY_HEADER = (os.getenv("MONITOR_APIKEY_HEADER", "apikey") or "apikey").strip()

# Endpoint real da Evolution (padrão): /instance/connectionState/{instance}
# Se quiser customizar via env, setar exatamente o path com {instance}
# Ex: /instance/connectionState/{instance}
MONITOR_CONNECTIONSTATE_PATH = (os.getenv("MONITOR_CONNECTIONSTATE_PATH", "/instance/connectionState/{instance}") or "").strip()


def _normalize_base_url(s: Optional[str]) -> str:
    return (s or "").strip().rstrip("/")


def _build_url(base_url: str, path_template: str, *, instance: str) -> str:
    base_url = _normalize_base_url(base_url)
    path = (path_template or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    path = path.replace("{instance}", instance)
    return f"{base_url}{path}"


def _classify_by_state(state: Optional[str]) -> str:
    """
    Evolution normalmente retorna state: open | connecting | close (e variações).
    """
    s = (state or "").strip().lower()
    if s == "open":
        return "online"
    if s in ("connecting", "qr", "qrcode", "pairing", "loading"):
        return "degraded"
    if s in ("close", "closed", "offline", "disconnected"):
        return "offline"
    return "unknown"


async def _check_one(agent: Agent, client: httpx.AsyncClient) -> Dict[str, Any]:
    """
    Check real da Evolution:
    GET /instance/connectionState/{instance}
    Header: apikey: <api-key>
    Resposta típica:
      { "instance": { "instanceName": "...", "state": "open" } }
    """
    base_url = _normalize_base_url(getattr(agent, "evolution_base_url", None))
    api_key = (getattr(agent, "api_key", None) or "").strip()
    instance = (getattr(agent, "instance", None) or "").strip()

    if not base_url:
        return {
            "status": "unknown",
            "latency_ms": None,
            "error": "missing_base_url",
            "details": {"reason": "agent.evolution_base_url vazio"},
        }

    if not instance:
        return {
            "status": "unknown",
            "latency_ms": None,
            "error": "missing_instance",
            "details": {"reason": "agent.instance vazio"},
        }

    url = _build_url(base_url, MONITOR_CONNECTIONSTATE_PATH, instance=instance)

    headers = {}
    if api_key:
        headers[MONITOR_APIKEY_HEADER] = api_key

    t0 = time.time()
    try:
        r = await client.get(url, headers=headers)
        ms = int(round((time.time() - t0) * 1000))

        details: Dict[str, Any] = {
            "url": url,
            "http_status": r.status_code,
            "instance": instance,
        }

        if 200 <= r.status_code < 300:
            data = None
            try:
                data = r.json()
            except Exception:
                data = None

            # tenta extrair state
            state = None
            if isinstance(data, dict):
                inst = data.get("instance")
                if isinstance(inst, dict):
                    state = inst.get("state")

            details["evolution"] = {"state": state, "raw": data if isinstance(data, dict) else None}

            # status base por state
            status = _classify_by_state(state)

            # aplica degraded por latência (mesmo se open)
            if status == "online" and ms >= MONITOR_DEGRADED_MS:
                status = "degraded"

            return {
                "status": status,
                "latency_ms": ms,
                "error": None if status != "unknown" else "unknown_state",
                "details": details,
            }

        # 401/403/404 etc
        return {
            "status": "offline",
            "latency_ms": ms,
            "error": f"http_{r.status_code}",
            "details": details,
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
            "details": {"url": url, "instance": instance, "exception": str(type(e).__name__)},
        }


def _save_check(agent_id: str, result: Dict[str, Any]) -> None:
    """
    Com agent_checks.id BIGSERIAL/Identity, não definimos id manualmente.
    """
    with SessionLocal() as db:
        row = AgentCheck(
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

    logger.info(
        "MONITOR_LOOP_START interval=%ss timeout=%ss path=%s",
        MONITOR_INTERVAL_SECONDS,
        MONITOR_TIMEOUT_SECONDS,
        MONITOR_CONNECTIONSTATE_PATH,
    )

    limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)

    async with httpx.AsyncClient(timeout=MONITOR_TIMEOUT_SECONDS, limits=limits) as client:
        while True:
            try:
                with SessionLocal() as db:
                    agents = db.execute(select(Agent).order_by(desc(Agent.created_at))).scalars().all()

                sem = asyncio.Semaphore(10)

                async def run_one(a: Agent) -> None:
                    async with sem:
                        res = await _check_one(a, client)
                        _save_check(str(a.id), res)

                if agents:
                    await asyncio.gather(*(run_one(a) for a in agents))

            except Exception as e:
                logger.exception("MONITOR_LOOP_ERROR: %s", e)

            await asyncio.sleep(MONITOR_INTERVAL_SECONDS)
