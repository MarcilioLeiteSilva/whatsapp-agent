# app/monitoring.py
from __future__ import annotations

import os
import time
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

import httpx
from sqlalchemy import select, desc
from .db import SessionLocal
from .models import Agent, AgentCheck

logger = logging.getLogger("agent")

# Config
MONITOR_ENABLED = os.getenv("MONITOR_ENABLED", "true").lower() in ("1", "true", "yes", "y")
MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "20"))
MONITOR_TIMEOUT_SECONDS = float(os.getenv("MONITOR_TIMEOUT_SECONDS", "4.0"))
MONITOR_DEGRADED_MS = int(os.getenv("MONITOR_DEGRADED_MS", "1500"))
MONITOR_OFFLINE_AFTER_FAILS = int(os.getenv("MONITOR_OFFLINE_AFTER_FAILS", "2"))

# Telegram (opcional)
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN", "") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID", "") or "").strip()
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "300"))  # 5 min

# estado em memória p/ evitar spam (bom o suficiente pro começo)
_last_alert_at: Dict[str, float] = {}
_last_status: Dict[str, str] = {}
_fail_streak: Dict[str, int] = {}


async def _telegram_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(url, json=payload)
            r.raise_for_status()
    except Exception as e:
        logger.error("TELEGRAM_SEND_ERROR: %s", e)


def _should_alert(key: str, new_status: str) -> bool:
    now = time.time()
    last_t = _last_alert_at.get(key, 0.0)
    last_s = _last_status.get(key, "unknown")

    # só alerta quando muda o status (ou se ficou offline e cooldown passou)
    if new_status != last_s:
        if now - last_t >= 5:  # micro proteção
            return True

    if new_status == "offline" and (now - last_t) >= ALERT_COOLDOWN_SECONDS:
        return True

    return False


async def _alert_if_needed(agent: Agent, status: str, latency_ms: Optional[int], error: Optional[str]) -> None:
    key = f"{agent.id}"
    if not _should_alert(key, status):
        _last_status[key] = status
        return

    _last_alert_at[key] = time.time()
    _last_status[key] = status

    name = getattr(agent, "name", None) or agent.instance
    inst = agent.instance
    client_id = agent.client_id

    if status == "online":
        msg = f"🟢 ONLINE: {name} ({inst}) client={client_id} latency={latency_ms}ms"
    elif status == "degraded":
        msg = f"🟡 DEGRADED: {name} ({inst}) client={client_id} latency={latency_ms}ms"
    else:
        msg = f"🔴 OFFLINE: {name} ({inst}) client={client_id} err={error or 'timeout'}"

    await _telegram_send(msg)


async def _check_one(agent: Agent) -> Dict[str, Any]:
    """
    Check simples e robusto:
    - tenta GET no evolution_base_url do agente (ou / se já responde)
    - você pode evoluir depois p/ endpoint mais confiável (ex: /health)
    """
    base = (getattr(agent, "evolution_base_url", None) or "").strip().rstrip("/")
    if not base or not base.startswith(("http://", "https://")):
        return {"status": "unknown", "latency_ms": None, "error": "missing_base_url"}

    url = base  # simples; pode virar f"{base}/health" se preferir

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=MONITOR_TIMEOUT_SECONDS) as c:
            r = await c.get(url)
            _ = r.status_code
        ms = int((time.time() - t0) * 1000)
        if ms >= MONITOR_DEGRADED_MS:
            return {"status": "degraded", "latency_ms": ms, "error": None}
        return {"status": "online", "latency_ms": ms, "error": None}
    except Exception as e:
        return {"status": "offline", "latency_ms": None, "error": str(e)}


def _persist_check(agent: Agent, mode: str, status: str, latency_ms: Optional[int], error: Optional[str]) -> None:
    with SessionLocal() as db:
        row = AgentCheck(
            client_id=agent.client_id,
            agent_id=agent.id,
            instance=agent.instance,
            mode=mode,
            status=status,
            latency_ms=latency_ms,
            error=(error or None),
        )
        db.add(row)
        db.commit()


async def poll_once() -> None:
    with SessionLocal() as db:
        agents: List[Agent] = db.execute(select(Agent).order_by(desc(Agent.created_at))).scalars().all()

    for a in agents:
        key = str(a.id)
        res = await _check_one(a)

        # streak de falhas p/ offline real (evita piscar)
        if res["status"] == "offline":
            _fail_streak[key] = _fail_streak.get(key, 0) + 1
        else:
            _fail_streak[key] = 0

        effective_status = res["status"]
        if res["status"] == "offline" and _fail_streak[key] < MONITOR_OFFLINE_AFTER_FAILS:
            effective_status = "degraded"  # “suspeito” antes do offline definitivo

        _persist_check(a, mode="poll", status=effective_status, latency_ms=res["latency_ms"], error=res["error"])

        await _alert_if_needed(a, effective_status, res["latency_ms"], res["error"])


async def monitoring_loop() -> None:
    if not MONITOR_ENABLED:
        logger.info("MONITOR: disabled")
        return

    logger.info("MONITOR: enabled interval=%ss timeout=%ss", MONITOR_INTERVAL_SECONDS, MONITOR_TIMEOUT_SECONDS)

    # loop infinito
    while True:
        try:
            await poll_once()
        except Exception as e:
            logger.error("MONITOR_LOOP_ERROR: %s", e)
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)
