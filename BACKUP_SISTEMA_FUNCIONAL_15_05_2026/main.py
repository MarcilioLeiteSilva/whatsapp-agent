import os
import time
import logging
import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import Response

from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from .evolution import EvolutionClient
from .store import MemoryStore
from .rules import reply_for, detect_intents
from .lead_logger import (
    ensure_first_contact,
    mark_intent,
    save_handoff_lead,
    get_agent_by_instance
)

from .metrics import (
    WEBHOOK_RECEIVED,
    WEBHOOK_IGNORED,
    MSG_PROCESSED,
    MSG_SENT_OK,
    MSG_SENT_ERR,
    WEBHOOK_LATENCY,
)

from .ratelimit import RateLimiter
from .admin import router as admin_router
from .integration import router as integration_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent")

app = FastAPI()
evo = EvolutionClient()
store = MemoryStore()
rl = RateLimiter(max_events=10, window_seconds=12)

# Incluindo os roteadores com seus prefixos corretos
app.include_router(admin_router)
app.include_router(integration_router)

async def notify_consigo(closing_id: int, data: dict, raw_text: str, number: str, instance: str):
    from .settings import CONSIGO_WEBHOOK_URL
    if not CONSIGO_WEBHOOK_URL: return
    
    payload = {
        "event": "inventory_result",
        "closing_id": closing_id,
        "instance_name": instance,
        "pdv_phone": number,
        "items": data.get("items", []),
        "raw_message": raw_text
    }
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.post(CONSIGO_WEBHOOK_URL, json=payload)
            logger.info(f"Webhook sent to Consigo: {r.status_code}")
        except Exception as e:
            logger.error(f"Error sending webhook: {e}")

def extract_text(msg: dict) -> str:
    if not isinstance(msg, dict): return ""
    if msg.get("conversation"): return msg.get("conversation")
    etm = msg.get("extendedTextMessage") or {}
    return etm.get("text") or ""

def extract_payload(payload: dict):
    instance = (payload.get("instance") or payload.get("instanceId") or "").strip()
    d = payload.get("data") or payload
    if isinstance(d, list) and len(d) > 0: d = d[0]
    if isinstance(d, dict) and isinstance(d.get("messages"), list) and d["messages"]:
        d0 = d["messages"][0]
        if isinstance(d0, dict): d = d0
    
    key = d.get("key") or {}
    message_id = key.get("id") or d.get("id") or ""
    remote = key.get("remoteJid") or d.get("from") or ""
    from_number = remote.replace("@s.whatsapp.net", "").replace("@c.us", "").strip()
    
    msg = d.get("message") or d.get("msg") or {}
    text = extract_text(msg).strip()
    from_me = bool(key.get("fromMe"))
    is_group = "@g.us" in remote
    event = (payload.get("event") or "").lower()
    status = (d.get("status") or "").upper()
    
    return instance, message_id, from_number, text, from_me, is_group, event, status

@app.post("/webhook")
async def webhook(req: Request, background_tasks: BackgroundTasks):
    start = time.time()
    WEBHOOK_RECEIVED.inc()
    try:
        payload = await req.json()
    except: return {"ok": True}

    instance, message_id, number, text, from_me, is_group, event, status = extract_payload(payload)
    if from_me or is_group: return {"ok": True}

    # Check de Pausa (Silêncio pós-acerto)
    if store.is_paused(number):
        logger.info("BOT_PAUSED: ignoring number=%s", number)
        return {"ok": True}

    agent = get_agent_by_instance(instance)
    if not agent: return {"ok": True}

    state = store.get_state(number)
    reply = await reply_for(number, text, state, agent=agent)
    
    if reply:
        if state.get("step") == "inventory_completed" and not state.get("notified_consigo"):
            background_tasks.add_task(notify_consigo, state.get("closing_id"), state.get("inventory_data"), text, number, instance)
            state["notified_consigo"] = True
            store.set_paused(number, 31536000)
            state.clear()
        
        store.save_state(number, state)
        await evo.send_text(instance, number, reply)
        MSG_SENT_OK.inc()
    
    WEBHOOK_LATENCY.observe(time.time() - start)
    return {"ok": True}
