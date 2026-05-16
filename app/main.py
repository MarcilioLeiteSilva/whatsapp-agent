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
    get_last_leads,
)

from .metrics import (
    WEBHOOK_RECEIVED,
    WEBHOOK_IGNORED,
    MSG_PROCESSED,
    MSG_SENT_OK,
    MSG_SENT_ERR,
    LEAD_FIRST_CONTACT,
    LEAD_INTENT_MARKED,
    LEAD_SAVED,
    WEBHOOK_LATENCY,
)

from .ratelimit import RateLimiter
from .admin import router as admin_router
from .integration import router as integration_router


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent")

ADMIN_NUMBER = os.getenv("ADMIN_NUMBER", "").strip()

app = FastAPI()
evo = EvolutionClient()
store = MemoryStore()
rl = RateLimiter(max_events=10, window_seconds=12)

async def notify_consigo(closing_id: int, data: dict, raw_text: str, number: str, instance: str):
    from .settings import CONSIGO_WEBHOOK_URL
    if not CONSIGO_WEBHOOK_URL:
        return
    
    payload = {
        "event": "inventory_result",
        "closing_id": closing_id,
        "instance_name": instance,
        "pdv_phone": number,
        "items": data.get("items", []),
        "notes": data.get("notes", ""),
        "raw_message": raw_text
    }
    
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.post(CONSIGO_WEBHOOK_URL, json=payload)
            logger.info(f"Webhook sent to Consigo: status={r.status_code}")
        except Exception as e:
            logger.error(f"Error sending webhook to Consigo: {e}")

app.include_router(admin_router)
app.include_router(integration_router)


def extract_text(msg: dict) -> str:
    """Extrai texto de diferentes formatos de mensagens WhatsApp."""
    if not isinstance(msg, dict):
        return ""

    # texto simples
    if msg.get("conversation"):
        return msg.get("conversation") or ""

    # texto longo / reply
    etm = msg.get("extendedTextMessage") or {}
    if isinstance(etm, dict) and etm.get("text"):
        return etm.get("text") or ""

    # respostas de botões
    brm = msg.get("buttonsResponseMessage") or {}
    if isinstance(brm, dict):
        if brm.get("selectedDisplayText"):
            return brm.get("selectedDisplayText") or ""
        if brm.get("selectedButtonId"):
            return brm.get("selectedButtonId") or ""

    # respostas de listas
    lrm = msg.get("listResponseMessage") or {}
    if isinstance(lrm, dict) and lrm.get("title"):
        return lrm.get("title") or ""

    return ""


def extract_payload(payload: dict):
    """
    Normaliza payload do webhook da Evolution (e variações) para:
    instance, message_id, from_number, text, from_me, is_group, event, status
    """
    instance = (payload.get("instance") or payload.get("instanceId") or "").strip()

    d = payload.get("data") or payload

    # algumas variações: data["messages"][0]
    if isinstance(d, dict) and isinstance(d.get("messages"), list) and d["messages"]:
        d0 = d["messages"][0] or {}
        if isinstance(d0, dict):
            d = d0

    key = (d.get("key") or {}) if isinstance(d, dict) else {}

    message_id = (key.get("id") or d.get("messageId") or d.get("id") or "").strip()

    remote = (
        (key.get("remoteJid") or d.get("remoteJid") or d.get("from") or "")
        if isinstance(d, dict)
        else ""
    ).strip()

    from_number = (
        remote.replace("@s.whatsapp.net", "")
        .replace("@c.us", "")
        .replace("whatsapp:", "")
        .strip()
    )

    msg = (d.get("message") or d.get("msg") or {}) if isinstance(d, dict) else {}
    text = extract_text(msg).strip()

    from_me = bool(key.get("fromMe") or (d.get("fromMe") if isinstance(d, dict) else False))
    is_group = remote.endswith("@g.us")

    event = (payload.get("event") or "").lower()
    status = (d.get("status") or "").upper()

    return instance, message_id, from_number, text, from_me, is_group, event, status


@app.get("/health")
def health():
    return {"status": "ok", "time": time.time()}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/webhook")
async def webhook(req: Request, background_tasks: BackgroundTasks):
    start = time.time()
    WEBHOOK_RECEIVED.inc()

    try:
        payload = await req.json()
    except Exception:
        WEBHOOK_IGNORED.labels("bad_json").inc()
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "ignored": "bad_json"}

    logger.info("WEBHOOK: %s", payload)

    instance, message_id, number, text, from_me, is_group, event, status = extract_payload(payload)

    from .lead_logger import get_agent_by_instance

    agent = get_agent_by_instance(instance)

    if not agent:
        logger.warning("UNKNOWN_INSTANCE: %s", instance)
        return {"ok": True, "ignored": "unknown_instance"}

    client_id = agent.client_id
    agent_id = agent.id

    logger.info(
        "EXTRACTED: instance=%s id=%s number=%s text=%r from_me=%s group=%s event=%s status=%s",
        instance,
        message_id,
        number,
        text,
        from_me,
        is_group,
        event,
        status,
    )

    # -------------------------
    # ✅ Filtrar ACK/status/update (ruído)
    # -------------------------
    if "update" in event:
        WEBHOOK_IGNORED.labels("update").inc()
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "ignored": "update"}

    if status in {
        "ACK",
        "READ",
        "DELIVERED",
        "DELIVERED_TO_DEVICE",
        "SERVER_ACK",
        "DELIVERY_ACK",
    } and not text:
        WEBHOOK_IGNORED.labels("ack/status_no_text").inc()
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "ignored": "ack/status_no_text"}

    # ignora mensagens enviadas por nós ou em grupo
    if from_me or is_group:
        WEBHOOK_IGNORED.labels("from_me_or_group").inc()
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "ignored": "from_me/group"}

    # dedup só se tiver id
    if message_id and store.seen(message_id):
        WEBHOOK_IGNORED.labels("dedup").inc()
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "ignored": "dedup"}

    # se não conseguiu extrair número/texto, não segue
    if not number or not text:
        WEBHOOK_IGNORED.labels("missing_number_or_text").inc()
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "ignored": "missing_number_or_text"}

    # Rate limit por número (proteção anti-spam)
    if not rl.allow(number):
        WEBHOOK_IGNORED.labels("rate_limited").inc()
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "ignored": "rate_limited"}

    # ---------------------------------------------------------
    # 🔒 PORTARIA: Somente ignora se a sessão estiver EXPLICITAMENTE fechada
    # ---------------------------------------------------------
    state = store.get_state(number)
    if state.get("status") == "closed":
        logger.info("SESSION_CLOSED: ignoring number=%s", number)
        return {"ok": True, "session": "closed"}

    MSG_PROCESSED.inc()

    # ================================
    # ✅ Captura automática
    # 1) Primeiro contato
    # 2) Intenção (lead quente)
    # ================================
    try:
        ensure_first_contact(
            client_id=client_id,
            agent_id=agent_id,
            instance=instance,
            from_number=number,
        )

        intents = detect_intents(text)
        for it in intents:
            mark_intent(client_id, agent_id, instance, number, it)
    except Exception as e:
        logger.error("CAPTURE_ERROR: %s", e)

    # ========================================
    # 🚦 Roteamento de Fluxo (State Router)
    # ========================================
    old_step = state.get("step")
    reply = await reply_for(number, text, state, agent=agent)
    new_step = state.get("step")

    if old_step != new_step:
        logger.info("STATE_TRANSITION: number=%s from=%s to=%s", number, old_step, new_step)
    
    logger.info("RULES_REPLY: number=%s reply=%r step=%s", number, reply, new_step)

    if reply is None:
        WEBHOOK_IGNORED.labels("paused").inc()
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "paused": True}

    # =========================================================
    # 🔔 Notificar Consigo se o inventário foi concluído
    # =========================================================
    if state.get("step") == "finished" and not state.get("notified_consigo"):
        background_tasks.add_task(
            notify_consigo,
            closing_id=state.get("closing_id"),
            data=state.get("inventory_data"),
            raw_text=text,
            number=number,
            instance=instance
        )
        state["notified_consigo"] = True
        # O estado agora permanece como status='closed' e step='finished' 
        # A portaria no início do webhook cuidará de ignorar as próximas mensagens.
        store.save_state(number, state)

    # ========================================
    # 💾 Salvar lead no Postgres (uma vez só)
    # (quando rules.py marcar step lead_captured)
    # ========================================
    try:
        if (
            state.get("step") == "lead_captured"
            and state.get("lead")
            and not state.get("lead_saved")
        ):
            lead = state["lead"] or {}
            nome = (lead.get("nome") or "").strip()
            telefone = (lead.get("telefone") or "").strip()
            assunto = (lead.get("assunto") or "").strip()

            save_handoff_lead(
                client_id=client_id,
                agent_id=agent_id,
                instance=instance,
                from_number=number,
                nome=nome,
                telefone=telefone,
                assunto=assunto
            )

            state["lead_saved"] = True
            LEAD_SAVED.inc()
            logger.info("LEAD_SAVED: instance=%s number=%s", instance, number)
    except Exception as e:
        logger.error("LEAD_SAVE_ERROR: %s", e)

    # ========================================
    # 📤 Envio de resposta (nunca derrubar webhook)
    # ========================================
    logger.info("SEND_TEXT: to=%s chars=%s", number, len(reply or ""))
    
    # Salva o estado atualizado no Banco
    store.save_state(number, state)

    try:
        await evo.send_text(instance, number, reply)
        MSG_SENT_OK.inc()
        logger.info("SEND_OK: number=%s", number)
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "sent": True}
    except Exception as e:
        MSG_SENT_ERR.inc()
        logger.error("SEND_TEXT_ERROR: %s", e)
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "sent": False}
