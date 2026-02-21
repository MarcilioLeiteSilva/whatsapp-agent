import os
import time
import logging
import httpx
from fastapi import FastAPI, Request
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


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent")

ADMIN_NUMBER = os.getenv("ADMIN_NUMBER", "").strip()

app = FastAPI()
evo = EvolutionClient()
store = MemoryStore()
rl = RateLimiter(max_events=10, window_seconds=12)

app.include_router(admin_router)


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

    # respostas de lista
    lrm = msg.get("listResponseMessage") or {}
    if isinstance(lrm, dict):
        ssr = lrm.get("singleSelectReply") or {}
        if isinstance(ssr, dict) and ssr.get("selectedRowId"):
            return ssr.get("selectedRowId") or ""
        if lrm.get("title"):
            return lrm.get("title") or ""

    # mídia com legenda
    for k in ("imageMessage", "videoMessage", "documentMessage"):
        m = msg.get(k) or {}
        if isinstance(m, dict) and m.get("caption"):
            return m.get("caption") or ""

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
    status = ((d.get("status") if isinstance(d, dict) else None) or payload.get("status") or "").upper()

    return instance, message_id, from_number, text, from_me, is_group, event, status


@app.get("/")
async def root():
    return {"ok": True}


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/status")
async def status():
    # DB check (usa a mesma infra do app)
    db_ok = True
    db_err = None
    try:
        _ = get_last_leads(limit=1)
    except Exception as e:
        db_ok = False
        db_err = str(e)

    # Evolution reachability check
    evo_ok = True
    evo_err = None
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(evo.base)
            _ = r.status_code
    except Exception as e:
        evo_ok = False
        evo_err = str(e)

    return {
        "ok": db_ok and evo_ok,
        "db_ok": db_ok,
        "db_err": db_err,
        "evolution_ok": evo_ok,
        "evolution_err": evo_err,
    }


@app.post("/webhook")
async def webhook(req: Request):
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
    # ✅ Filtrar ACK/status/update (ruído)
# Importante: a Evolution pode enviar messages.upsert com status (ex: DELIVERY_ACK)
# junto com uma mensagem real. Então só ignoramos "status" quando NÃO há texto.
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
        from_number=number
    )
        LEAD_FIRST_CONTACT.inc()

        intents = detect_intents(text)
        if intents:
            mark_intent(
        client_id=client_id,
        agent_id=agent_id,
        instance=instance,
        from_number=number,
        intents=intents
)
            
            LEAD_INTENT_MARKED.inc()
    except Exception as e:
        # Não derruba o atendimento se o banco falhar
        logger.error("LEAD_CAPTURE_ERROR: %s", e)

    state = store.get_state(number)

    # ========================================
    # 🔐 Comando ADMIN: listar últimos leads
    # ========================================
    if (text or "").strip().lower() == "#leads":
        if not ADMIN_NUMBER or number != ADMIN_NUMBER:
            WEBHOOK_IGNORED.labels("admin_unauthorized").inc()
            WEBHOOK_LATENCY.observe(time.time() - start)
            return {"ok": True}

        try:
            leads = get_last_leads(limit=5)
        except Exception as e:
            logger.error("ADMIN_LEADS_ERROR: %s", e)
            try:
                await evo.send_text(number, "Erro ao consultar leads no banco.")
            except Exception as se:
                logger.error("SEND_TEXT_ERROR(admin): %s", se)
            WEBHOOK_LATENCY.observe(time.time() - start)
            return {"ok": True}

        if not leads:
            try:
                await evo.send_text(number, "Nenhum lead encontrado.")
            except Exception as se:
                logger.error("SEND_TEXT_ERROR(admin): %s", se)
            WEBHOOK_LATENCY.observe(time.time() - start)
            return {"ok": True}

        msg = "📋 Últimos Leads:\n\n"
        for l in leads:
            msg += (
                f"👤 {l.get('nome') or '-'}\n"
                f"📞 {l.get('telefone') or '-'}\n"
                f"📝 {l.get('assunto') or '-'}\n"
                f"🕒 {l.get('created_at') or '-'}\n"
                f"🏷️ {l.get('status') or '-'} | {l.get('origem') or '-'}\n\n"
            )

        try:
            await evo.send_text(number, msg[:3500])
            MSG_SENT_OK.inc()
        except Exception as se:
            MSG_SENT_ERR.inc()
            logger.error("SEND_TEXT_ERROR(admin): %s", se)

        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True}

    # ========================================
    # 🤖 Regras normais do bot
    # ========================================
    reply = reply_for(number, text, state)
    logger.info("RULES_REPLY: number=%s reply=%r step=%s", number, reply, (state or {}).get("step"))

    if reply is None:
        WEBHOOK_IGNORED.labels("paused").inc()
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "paused": True}

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

    try:
        await evo.send_text(number, reply)
        MSG_SENT_OK.inc()
        logger.info("SEND_OK: number=%s", number)
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "sent": True}
    except Exception as e:
        MSG_SENT_ERR.inc()
        logger.error("SEND_TEXT_ERROR: %s", e)
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "sent": False}
    
