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
    from .settings import CONSIGO_WEBHOOK_URL, INTEGRATION_KEY
    # PASSO 1: Garante que a URL tenha o caminho correto (SINGULAR)
    base_url = CONSIGO_WEBHOOK_URL.rstrip("/")
    target_url = f"{base_url}/webhook/whatsapp/inventory"
    
    logger.info(f"[WEBHOOK_LOG] Attempting to send result to: {target_url}")
    
    payload = {
        "event": "inventory_result",
        "closing_id": closing_id,
        "instance_name": instance,
        "pdv_phone": number,
        "items": data.get("items", []),
        "notes": raw_text
    }
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.post(target_url, json=payload, headers={"x-integration-key": INTEGRATION_KEY})
            logger.info(f"[WEBHOOK_LOG] Delivery result: {r.status_code}")
        except Exception as e:
            logger.error(f"[WEBHOOK_LOG] Delivery failed: {e}")

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

    # [WEBHOOK_LOG] Início do processamento
    instance, message_id, number, text, from_me, is_group, event, status = extract_payload(payload)
    
    # PASSO 1: Filtrar apenas eventos de novas mensagens (ignora ACKs, updates, etc.)
    allowed_events = ["messages.upsert", "messages_upsert"]
    if event not in allowed_events:
        return {"ok": True}
        
    if from_me or is_group: return {"ok": True}
    
    # PASSO 2: Validar telefone antes de qualquer ação
    if not number or len(number) < 5:
        # Silenciamos o erro para não poluir o log, apenas ignoramos
        return {"ok": True}

    # PASSO 3: Bloquear mensagens após CLOSED (Check de Pausa)
    if store.is_paused(number):
        logger.info(f"[SETTLEMENT_LOG] BOT_PAUSED: Ignoring number={number}")
        return {"ok": True}

    agent = get_agent_by_instance(instance)
    if not agent: return {"ok": True}

    state = store.get_state(number)
    reply = await reply_for(number, text, state, agent=agent)
    
    if reply:
        # PASSO 2: Salvar estado e disparar ações ANTES de limpar ou fechar
        store.save_state(number, state)
        
        if state.get("step") == "inventory_completed" and not state.get("notified_consigo"):
            logger.info(f"[SETTLEMENT_LOG] Inventory completed for {number}. Dispatched to Consigo.")
            background_tasks.add_task(notify_consigo, state.get("closing_id"), state.get("inventory_data"), text, number, instance)
            state["notified_consigo"] = True
            
            # PASSO 2 & 3: Enviamos a mensagem FINAL antes de limpar o estado
            try:
                await evo.send_text(instance, number, reply)
                logger.info(f"[EVOLUTION_LOG] Final message sent to {number}")
                MSG_SENT_OK.inc()
            except Exception as e:
                logger.error(f"[EVOLUTION_LOG] Failed to send final message: {e}")
            
            # Somente AGORA limpamos e pausamos
            store.set_paused(number, 31536000)
            state.clear()
            store.save_state(number, state)
        else:
            # Fluxo normal de conversa
            try:
                await evo.send_text(instance, number, reply)
                MSG_SENT_OK.inc()
            except Exception as e:
                logger.error(f"[EVOLUTION_LOG] Error sending message: {e}")
            
            store.save_state(number, state)
    
    WEBHOOK_LATENCY.observe(time.time() - start)
    return {"ok": True}
