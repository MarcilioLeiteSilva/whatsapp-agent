import os
import time
import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

from .admin_web import router as admin_web_router
app.include_router(admin_web_router)

from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from .models import Client, Agent, Lead
from .evolution import EvolutionClient
from .store import MemoryStore
from .rules import reply_for, detect_intents
from .lead_logger import (
    ensure_first_contact,
    mark_intent,
    save_handoff_lead,
    get_last_leads,
    get_agent_by_instance,
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

# (Opcional) DEV auto-create tables
try:
    from .db_init import init_db_if_dev
except Exception:
    init_db_if_dev = None

# (Opcional) DEV bootstrap endpoint
try:
    from .admin_bootstrap import router as admin_bootstrap_router
except Exception:
    admin_bootstrap_router = None


# -----------------------------------------------------------------------------
# Logging / Config
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent")

ADMIN_NUMBER = os.getenv("ADMIN_NUMBER", "").strip()
ALLOW_SIMULATOR = os.getenv("ALLOW_SIMULATOR", "false").strip().lower() in ("1", "true", "yes", "y")


# -----------------------------------------------------------------------------
# App & singletons
# -----------------------------------------------------------------------------
app = FastAPI()
evo = EvolutionClient()
store = MemoryStore()
rl = RateLimiter(max_events=10, window_seconds=12)

app.include_router(admin_web_router)

app.include_router(admin_router)
if admin_bootstrap_router:
    app.include_router(admin_bootstrap_router)


@app.on_event("startup")
async def on_startup():
    # DEV-only: cria tabelas automaticamente se você adotou db_init.py
    if init_db_if_dev:
        init_db_if_dev()


# -----------------------------------------------------------------------------
# Helpers: parsing / normalization
# -----------------------------------------------------------------------------
def extract_text(msg: dict) -> str:
    """Extrai texto de diferentes formatos de mensagens WhatsApp."""
    if not isinstance(msg, dict):
        return ""

    # Texto simples
    if msg.get("conversation"):
        return msg.get("conversation") or ""

    # Texto longo / reply
    etm = msg.get("extendedTextMessage") or {}
    if isinstance(etm, dict) and etm.get("text"):
        return etm.get("text") or ""

    # Respostas de botões
    brm = msg.get("buttonsResponseMessage") or {}
    if isinstance(brm, dict):
        if brm.get("selectedDisplayText"):
            return brm.get("selectedDisplayText") or ""
        if brm.get("selectedButtonId"):
            return brm.get("selectedButtonId") or ""

    # Respostas de lista
    lrm = msg.get("listResponseMessage") or {}
    if isinstance(lrm, dict):
        ssr = lrm.get("singleSelectReply") or {}
        if isinstance(ssr, dict) and ssr.get("selectedRowId"):
            return ssr.get("selectedRowId") or ""
        if lrm.get("title"):
            return lrm.get("title") or ""

    # Mídia com legenda
    for k in ("imageMessage", "videoMessage", "documentMessage"):
        m = msg.get(k) or {}
        if isinstance(m, dict) and m.get("caption"):
            return m.get("caption") or ""

    return ""


def extract_payload(payload: dict):
    """
    Normaliza payload do webhook da Evolution (e variações) para:
    instance, message_id, from_number, text, from_me, is_group, event, status

    Observação:
    - A Evolution pode enviar eventos com "data.messages[0]" ou com "data.key".
    - Também pode chegar "status" junto do evento.
    """
    instance = (payload.get("instance") or payload.get("instanceId") or "").strip()

    d = payload.get("data") or payload

    # Algumas variações: data["messages"][0]
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


# -----------------------------------------------------------------------------
# Basic endpoints
# -----------------------------------------------------------------------------
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
    """
    Endpoint de diagnóstico rápido.
    - DB check: usa get_last_leads() para validar conexão/queries.
    - Evolution check:
        1) tenta usar EVOLUTION_BASE_URL (compat)
        2) se não existir/for inválida, tenta pegar um Agent do DB que tenha evolution_base_url
    """
    # DB check
    db_ok = True
    db_err = None
    try:
        _ = get_last_leads(limit=1)
    except Exception as e:
        db_ok = False
        db_err = str(e)

    # Evolution reachability check (tolerante e mais informativo)
    evo_ok = True
    evo_err = None
    try:
        base = (getattr(evo, "base", "") or "").strip()

        # Se não tem base global, tenta pegar do DB (SaaS mode)
        if not base or not (base.startswith("http://") or base.startswith("https://")):
            try:
                # Import local para evitar custo/risco de circular import no startup
                from sqlalchemy import select
                from .db import SessionLocal
                from .models import Agent

                with SessionLocal() as db:
                    a = db.execute(
                        select(Agent)
                        .where(Agent.evolution_base_url.is_not(None))
                        .limit(1)
                    ).scalar_one_or_none()

                if a and (a.evolution_base_url or "").strip():
                    base = (a.evolution_base_url or "").strip().rstrip("/")
            except Exception:
                # Se falhar a leitura do DB, cai para validação do base original mesmo
                pass

        if not (base.startswith("http://") or base.startswith("https://")):
            raise ValueError(f"EVOLUTION_BASE_URL inválida (sem protocolo): {base!r}")

        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(base)
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
        "allow_simulator": ALLOW_SIMULATOR,
    }


# -----------------------------------------------------------------------------
# Webhook (core)
# -----------------------------------------------------------------------------
@app.post("/webhook")
async def webhook(req: Request):
    """
    Webhook da Evolution: recebe eventos (messages.upsert, etc).

    Estratégia:
    1) Parse do payload e normalização.
    2) Resolver agente via instance (multi-tenant/multi-agente).
    3) Filtrar ruído (ACK/status sem texto, updates, fromMe, grupos).
    4) Dedup por message_id + rate-limit por número.
    5) Captura automática (primeiro contato e intenção).
    6) Regras do bot (rules.py) + envio de resposta.
    7) Persistência do lead quando step == lead_captured (uma vez só).
    """
    start = time.time()
    WEBHOOK_RECEIVED.inc()

    try:
        payload = await req.json()
    except Exception:
        WEBHOOK_IGNORED.labels("bad_json").inc()
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "ignored": "bad_json"}

    instance, message_id, number, text, from_me, is_group, event, status = extract_payload(payload)

    # -------------------------------------------------------------------------
    # Multi-tenant routing: instance -> agent (client_id, agent_id)
    # -------------------------------------------------------------------------
    agent = get_agent_by_instance(instance)
    if not agent:
        logger.warning("UNKNOWN_INSTANCE: instance=%s", instance)
        WEBHOOK_IGNORED.labels("unknown_instance").inc()
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "ignored": "unknown_instance"}

    client_id = agent.client_id
    agent_id = agent.id

    # Sempre logar contexto do agente (multiagente)
    logger.info(
        "CTX: client_id=%s agent_id=%s instance=%s message_id=%s from=%s event=%s status=%s text=%r",
        client_id,
        agent_id,
        instance,
        message_id,
        number,
        event,
        status,
        text,
    )

    # -------------------------------------------------------------------------
    # Filtrar ruído (ACK/status/update)
    # - Só ignoramos ACK/status quando NÃO há texto.
    # -------------------------------------------------------------------------
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

    # Ignora mensagens enviadas por nós ou em grupo
    if from_me or is_group:
        WEBHOOK_IGNORED.labels("from_me_or_group").inc()
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "ignored": "from_me/group"}

    # Dedup só se tiver id
    if message_id and store.seen(message_id):
        WEBHOOK_IGNORED.labels("dedup").inc()
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "ignored": "dedup"}

    # Se não conseguiu extrair número/texto, não segue
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

    # -------------------------------------------------------------------------
    # Captura automática:
    # 1) Primeiro contato
    # 2) Intenção (lead quente)
    # -------------------------------------------------------------------------
    try:
        ensure_first_contact(
            client_id=client_id,
            agent_id=agent_id,
            instance=instance,
            from_number=number,
        )
        LEAD_FIRST_CONTACT.inc()

        intents = detect_intents(text)
        if intents:
            mark_intent(
                client_id=client_id,
                agent_id=agent_id,
                instance=instance,
                from_number=number,
                intents=intents,
            )
            LEAD_INTENT_MARKED.inc()
    except Exception as e:
        logger.error("LEAD_CAPTURE_ERROR: client_id=%s agent_id=%s instance=%s err=%s", client_id, agent_id, instance, e)

    # Estado da conversa (memória curta)
    state = store.get_state(number)

    # -------------------------------------------------------------------------
    # Comando ADMIN: listar últimos leads
    # -------------------------------------------------------------------------
    if (text or "").strip().lower() == "#leads":
        if not ADMIN_NUMBER or number != ADMIN_NUMBER:
            WEBHOOK_IGNORED.labels("admin_unauthorized").inc()
            WEBHOOK_LATENCY.observe(time.time() - start)
            return {"ok": True}

        try:
            leads = get_last_leads(limit=5)
        except Exception as e:
            logger.error("ADMIN_LEADS_ERROR: client_id=%s agent_id=%s instance=%s err=%s", client_id, agent_id, instance, e)
            try:
                await evo.send_text(
                    number,
                    "Erro ao consultar leads no banco.",
                    base_url=agent.evolution_base_url,
                    instance=agent.instance,
                    api_key=agent.api_key,
                )
            except Exception as se:
                logger.error("SEND_TEXT_ERROR(admin): client_id=%s agent_id=%s instance=%s err=%s", client_id, agent_id, instance, se)
            WEBHOOK_LATENCY.observe(time.time() - start)
            return {"ok": True}

        if not leads:
            try:
                await evo.send_text(
                    number,
                    "Nenhum lead encontrado.",
                    base_url=agent.evolution_base_url,
                    instance=agent.instance,
                    api_key=agent.api_key,
                )
            except Exception as se:
                logger.error("SEND_TEXT_ERROR(admin): client_id=%s agent_id=%s instance=%s err=%s", client_id, agent_id, instance, se)
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
            await evo.send_text(
                number,
                msg[:3500],
                base_url=agent.evolution_base_url,
                instance=agent.instance,
                api_key=agent.api_key,
            )
            MSG_SENT_OK.inc()
        except Exception as se:
            MSG_SENT_ERR.inc()
            logger.error("SEND_TEXT_ERROR(admin): client_id=%s agent_id=%s instance=%s err=%s", client_id, agent_id, instance, se)

        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True}

    # -------------------------------------------------------------------------
    # Regras normais do bot (rules.py)
    # -------------------------------------------------------------------------
    reply = reply_for(number, text, state)
    logger.info(
        "RULES_REPLY: client_id=%s agent_id=%s instance=%s from=%s reply=%r step=%s",
        client_id,
        agent_id,
        instance,
        number,
        reply,
        (state or {}).get("step"),
    )

    # Quando rules.py retorna None, interpretamos como "pausado" (handoff humano)
    if reply is None:
        WEBHOOK_IGNORED.labels("paused").inc()
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "paused": True}

    # -------------------------------------------------------------------------
    # Persistência do lead (uma vez só)
    # -------------------------------------------------------------------------
    try:
        if (
            state.get("step") == "lead_captured"
            and state.get("lead")
            and not state.get("lead_saved")
        ):
            lead = state.get("lead") or {}
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
                assunto=assunto,
            )

            state["lead_saved"] = True
            LEAD_SAVED.inc()
            logger.info("LEAD_SAVED: client_id=%s agent_id=%s instance=%s from=%s", client_id, agent_id, instance, number)
    except Exception as e:
        logger.error("LEAD_SAVE_ERROR: client_id=%s agent_id=%s instance=%s err=%s", client_id, agent_id, instance, e)

    # -------------------------------------------------------------------------
    # Envio de resposta (SaaS: por agente)
    # -------------------------------------------------------------------------
    logger.info(
        "SEND_TEXT: client_id=%s agent_id=%s instance=%s to=%s chars=%s",
        client_id,
        agent_id,
        instance,
        number,
        len(reply or ""),
    )

    try:
        await evo.send_text(
            number,
            reply,
            base_url=agent.evolution_base_url,
            instance=agent.instance,
            api_key=agent.api_key,
        )
        MSG_SENT_OK.inc()
        logger.info("SEND_OK: client_id=%s agent_id=%s instance=%s to=%s", client_id, agent_id, instance, number)
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "sent": True}
    except Exception as e:
        MSG_SENT_ERR.inc()
        logger.error("SEND_TEXT_ERROR: client_id=%s agent_id=%s instance=%s err=%s", client_id, agent_id, instance, e)
        WEBHOOK_LATENCY.observe(time.time() - start)
        return {"ok": True, "sent": False}
    
