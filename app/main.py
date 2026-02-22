# ---------------------------------------------------------------------
# whatsapp-agent — app/main.py
# ---------------------------------------------------------------------
import os
import time
import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from .admin_bootstrap import router as admin_bootstrap_router
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


# -----------------------------------------------------------------------------
# Logging / Config
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent")

ADMIN_NUMBER = os.getenv("ADMIN_NUMBER", "").strip()

# -----------------------------------------------------------------------------
# DEV-only: Simulator (payload simplificado -> converte internamente)
# -----------------------------------------------------------------------------
ALLOW_SIMULATOR = os.getenv("ALLOW_SIMULATOR", "false").strip().lower() in ("1", "true", "yes", "y")
SIMULATOR_KEY = os.getenv("SIMULATOR_KEY", "").strip()


def _is_simulator_payload(payload: dict) -> bool:
    """
    Detecta payload do simulador.
    Regra: source == "simulator" OU presença de campos simplificados.
    """
    if not isinstance(payload, dict):
        return False
    return payload.get("source") == "simulator" or (
        "from_number" in payload and "text" in payload and "instance" in payload
    )


def _convert_simulator_to_evolution(payload: dict) -> dict:
    """
    Converte payload simplificado do simulador para um shape compatível com extract_payload().

    Mantém apenas o necessário:
    - instance
    - data.key.remoteJid / id / fromMe
    - data.message.conversation (texto)
    - status / event

    Observação:
    - Isso existe APENAS para DEV, para testar multiagentes sem depender da Evolution real.
    """
    instance = (payload.get("instance") or "").strip()
    message_id = (payload.get("message_id") or payload.get("id") or "").strip()
    from_number = (payload.get("from_number") or "").strip()
    text = (payload.get("text") or "").strip()
    status = (payload.get("status") or "MESSAGE").strip().upper()
    event = (payload.get("event") or "messages.upsert").strip().lower()

    remote_jid = f"{from_number}@s.whatsapp.net" if from_number else ""

    return {
        "event": event,
        "instance": instance,
        "data": {
            "key": {
                "remoteJid": remote_jid,
                "fromMe": False,
                "id": message_id or f"sim-{int(time.time())}",
                "participant": "",
                "addressingMode": "pn",
            },
            "pushName": payload.get("push_name") or "Simulator",
            "status": status,
            "message": {"conversation": text},
            "messageType": "conversation",
            "messageTimestamp": int(time.time()),
            "source": "simulator",
        },
    }


# -----------------------------------------------------------------------------
# App & singletons
# -----------------------------------------------------------------------------
app = FastAPI()
evo = EvolutionClient()
store = MemoryStore()
rl = RateLimiter(max_events=10, window_seconds=12)

app.include_router(admin_router)
app.include_router(admin_bootstrap_router)

# -----------------------------------------------------------------------------
# Helpers: parsing / normalization (Evolution payload)
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
# Endpoints básicos
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
    # DB check
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
        "allow_simulator": ALLOW_SIMULATOR,
    }


# -----------------------------------------------------------------------------
# Webhook (core)
# -----------------------------------------------------------------------------
@app.post("/webhook")
async def webhook(req: Request):
    """
    Webhook da Evolution (PROD) + payload simplificado do Simulator (DEV-only).

    Fluxo:
    1) Recebe JSON
    2) Se for simulador: valida chave (DEV-only) e converte para shape Evolution
    3) Normaliza payload (extract_payload)
    4) Resolve agent por instance (multiagente)
    5) Filtra ruído / dedup / rate-limit
    6) Captura automática (first_contact + intents)
    7) Regras (rules.py)
    8) Salva lead (handoff) quando step=lead_captured
    9) Envia mensagem via Evolution
    """
    start = time.time()
    WEBHOOK_RECEIVED.inc()

    try:
        payload = await req.json()
    except Exception:
        WEBHOOK_IGNORED.labels("bad_json").inc()
        return {"ok": True, "ignored": "bad_json"}
    finally:
        # não dá observe aqui porque ainda não sabemos se vamos retornar (teremos finally no final)
        pass

    # -------------------------------
    # DEV-only: Simulator
    # -------------------------------
    if _is_simulator_payload(payload):
        if not ALLOW_SIMULATOR:
            WEBHOOK_IGNORED.labels("simulator_disabled").inc()
            WEBHOOK_LATENCY.observe(time.time() - start)
            return {"ok": True, "ignored": "simulator_disabled"}

        key = (req.headers.get("X-SIMULATOR-KEY") or "").strip()
        if not SIMULATOR_KEY or key != SIMULATOR_KEY:
            WEBHOOK_IGNORED.labels("simulator_unauthorized").inc()
            WEBHOOK_LATENCY.observe(time.time() - start)
            return {"ok": True, "ignored": "simulator_unauthorized"}

        payload = _convert_simulator_to_evolution(payload)

    # Log reduzido (evita poluir/vazar payload gigante)
    logger.info("WEBHOOK_IN: event=%s instance=%s", (payload.get("event") or ""), (payload.get("instance") or ""))

    try:
        instance, message_id, number, text, from_me, is_group, event, status = extract_payload(payload)

        if not instance:
            WEBHOOK_IGNORED.labels("missing_instance").inc()
            return {"ok": True, "ignored": "missing_instance"}

        # ---------------------------------------------------------------------
        # Multi-tenant routing: instance -> agent (client_id, agent_id)
        # ---------------------------------------------------------------------
        agent = get_agent_by_instance(instance)
        if not agent:
            logger.warning("UNKNOWN_INSTANCE: instance=%s", instance)
            WEBHOOK_IGNORED.labels("unknown_instance").inc()
            return {"ok": True, "ignored": "unknown_instance"}

        client_id = agent.client_id
        agent_id = agent.id

        # Log sempre citando qual agente (client/agent/instance)
        logger.info(
            "CTX: client_id=%s agent_id=%s instance=%s msg_id=%s from=%s text=%r status=%s event=%s",
            client_id,
            agent_id,
            instance,
            message_id,
            number,
            text,
            status,
            event,
        )

        # ---------------------------------------------------------------------
        # Filtrar ruído (ACK/status/update)
        # Só ignora ACK/status quando NÃO há texto.
        # ---------------------------------------------------------------------
        if "update" in (event or ""):
            WEBHOOK_IGNORED.labels("update").inc()
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
            return {"ok": True, "ignored": "ack/status_no_text"}

        # Ignora mensagens enviadas por nós ou em grupo
        if from_me or is_group:
            WEBHOOK_IGNORED.labels("from_me_or_group").inc()
            return {"ok": True, "ignored": "from_me/group"}

        # Dedup só se tiver id
        if message_id and store.seen(message_id):
            WEBHOOK_IGNORED.labels("dedup").inc()
            return {"ok": True, "ignored": "dedup"}

        # Se não conseguiu extrair número/texto, não segue
        if not number or not text:
            WEBHOOK_IGNORED.labels("missing_number_or_text").inc()
            return {"ok": True, "ignored": "missing_number_or_text"}

        # Rate limit por número
        if not rl.allow(number):
            WEBHOOK_IGNORED.labels("rate_limited").inc()
            return {"ok": True, "ignored": "rate_limited"}

        MSG_PROCESSED.inc()

        # ---------------------------------------------------------------------
        # Captura automática (não derruba o fluxo se DB falhar)
        # ---------------------------------------------------------------------
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
            logger.error("LEAD_CAPTURE_ERROR: client_id=%s agent_id=%s err=%s", client_id, agent_id, e)

        # Estado da conversa
        state = store.get_state(number) or {}

        # ---------------------------------------------------------------------
        # Comando ADMIN: listar últimos leads (restrito)
        # ---------------------------------------------------------------------
        if (text or "").strip().lower() == "#leads":
            if not ADMIN_NUMBER or number != ADMIN_NUMBER:
                WEBHOOK_IGNORED.labels("admin_unauthorized").inc()
                return {"ok": True}

            try:
                leads = get_last_leads(limit=5, client_id=client_id, agent_id=agent_id)
            except Exception as e:
                logger.error("ADMIN_LEADS_ERROR: client_id=%s agent_id=%s err=%s", client_id, agent_id, e)
                try:
                    await evo.send_text(number, "Erro ao consultar leads no banco.")
                except Exception as se:
                    logger.error("SEND_TEXT_ERROR(admin): %s", se)
                return {"ok": True}

            if not leads:
                try:
                    await evo.send_text(number, "Nenhum lead encontrado.")
                except Exception as se:
                    logger.error("SEND_TEXT_ERROR(admin): %s", se)
                return {"ok": True}

            msg = f"📋 Últimos Leads ({client_id}/{agent_id}):\n\n"
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

            return {"ok": True}

        # ---------------------------------------------------------------------
        # Regras normais do bot
        # ---------------------------------------------------------------------
        reply = reply_for(number, text, state)
        logger.info(
            "RULES_REPLY: client_id=%s agent_id=%s instance=%s number=%s step=%s reply=%r",
            client_id,
            agent_id,
            instance,
            number,
            state.get("step"),
            reply,
        )

        # None = pausado (handoff humano)
        if reply is None:
            WEBHOOK_IGNORED.labels("paused").inc()
            return {"ok": True, "paused": True}

        # ---------------------------------------------------------------------
        # Persistência do lead (quando rules.py marcar lead_captured)
        # ---------------------------------------------------------------------
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
                logger.info("LEAD_SAVED: client_id=%s agent_id=%s instance=%s number=%s", client_id, agent_id, instance, number)
        except Exception as e:
            logger.error("LEAD_SAVE_ERROR: client_id=%s agent_id=%s err=%s", client_id, agent_id, e)

        # ---------------------------------------------------------------------
        # Envio de resposta via Evolution
        # ---------------------------------------------------------------------
        logger.info("SEND_TEXT: client_id=%s agent_id=%s instance=%s to=%s chars=%s", client_id, agent_id, instance, number, len(reply or ""))

        try:
            await evo.send_text(number, reply)
            MSG_SENT_OK.inc()
            logger.info("SEND_OK: client_id=%s agent_id=%s instance=%s number=%s", client_id, agent_id, instance, number)
            return {"ok": True, "sent": True}
        except Exception as e:
            MSG_SENT_ERR.inc()
            logger.error("SEND_TEXT_ERROR: client_id=%s agent_id=%s err=%s", client_id, agent_id, e)
            return {"ok": True, "sent": False}

    finally:
        # Garante métrica de latência em TODOS os caminhos
        WEBHOOK_LATENCY.observe(time.time() - start)
    
