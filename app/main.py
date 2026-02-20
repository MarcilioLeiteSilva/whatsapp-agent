import os
import logging
from fastapi import FastAPI, Request

from .evolution import EvolutionClient
from .store import MemoryStore
from .rules import reply_for, detect_intents
from .lead_logger import (
    ensure_first_contact,
    mark_intent,
    save_handoff_lead,
    get_last_leads,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent")

ADMIN_NUMBER = os.getenv("ADMIN_NUMBER", "").strip()

app = FastAPI()
evo = EvolutionClient()
store = MemoryStore()


def extract_text(msg: dict) -> str:
    """
    Extrai texto de diferentes formatos de mensagens WhatsApp.
    Mantém compatibilidade com variações de payload (conversation, extendedTextMessage, botões, listas, mídia com caption).
    """
    if not isinstance(msg, dict):
        return ""

    # texto simples
    if msg.get("conversation"):
        return msg.get("conversation") or ""

    # texto longo / reply
    etm = msg.get("extendedTextMessage") or {}
    if isinstance(etm, dict) and etm.get("text"):
        return etm.get("text") or ""

    # respostas de botões (dependendo do provedor)
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
    Normaliza o payload do webhook da Evolution (ou variações) para:
    instance, message_id, from_number, text, from_me, is_group, event, status
    """
    # instance pode vir em chaves diferentes
    instance = (payload.get("instance") or payload.get("instanceId") or "").strip()

    # muitos provedores colocam dentro de payload["data"]
    d = payload.get("data") or payload

    # algumas variações mandam lista: data["messages"][0]
    if isinstance(d, dict) and isinstance(d.get("messages"), list) and d["messages"]:
        d0 = d["messages"][0] or {}
        if isinstance(d0, dict):
            d = d0

    key = (d.get("key") or {}) if isinstance(d, dict) else {}

    # id pode variar de campo
    message_id = (key.get("id") or d.get("messageId") or d.get("id") or "").strip()

    # remoteJid pode variar de campo
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

    # mensagem pode variar de campo
    msg = (d.get("message") or d.get("msg") or {}) if isinstance(d, dict) else {}
    text = extract_text(msg).strip()

    from_me = bool(key.get("fromMe") or (d.get("fromMe") if isinstance(d, dict) else False))
    is_group = remote.endswith("@g.us")

    event = (payload.get("event") or "").lower()
    status = ((d.get("status") if isinstance(d, dict) else None) or payload.get("status") or "").upper()

    return instance, message_id, from_number, text, from_me, is_group, event, status


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/webhook")
async def webhook(req: Request):
    payload = await req.json()
    logger.info("WEBHOOK: %s", payload)

    instance, message_id, number, text, from_me, is_group, event, status = extract_payload(payload)

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
    # ✅ Filtros de eventos ACK/update
    # -------------------------
    # Ex.: messages.update / deliveries / read receipts etc.
    if "update" in event or status in {
        "ACK",
        "READ",
        "DELIVERED",
        "DELIVERED_TO_DEVICE",
        "SERVER_ACK",
        "DELIVERY_ACK",
    }:
        return {"ok": True, "ignored": "ack/status"}
        
    # ignora mensagens enviadas por nós ou em grupo
    if from_me or is_group:
        return {"ok": True, "ignored": "from_me/group"}

    # dedup: só aplica se tiver id
    if message_id and store.seen(message_id):
        return {"ok": True, "ignored": "dedup"}

    # se não conseguiu extrair número/texto, não segue
    if not number or not text:
        return {"ok": True, "ignored": "missing_number_or_text"}

    # ================================
    # ✅ Captura automática (D)
    # 1) Primeiro contato
    # 2) Intenção (lead quente)
    # ================================
    try:
        ensure_first_contact(instance=instance, from_number=number)

        intents = detect_intents(text)
        if intents:
            mark_intent(instance=instance, from_number=number, intents=intents)
    except Exception as e:
        # Não derruba o atendimento se o banco falhar
        logger.error("LEAD_CAPTURE_ERROR: %s", e)

    state = store.get_state(number)

    # ========================================
    # 🔐 Comando ADMIN: listar últimos leads
    # ========================================
    if (text or "").strip().lower() == "#leads":
        if not ADMIN_NUMBER or number != ADMIN_NUMBER:
            return {"ok": True}

        try:
            leads = get_last_leads(limit=5)
        except Exception as e:
            logger.error("ADMIN_LEADS_ERROR: %s", e)
            await evo.send_text(number, "Erro ao consultar leads no banco.")
            return {"ok": True}

        if not leads:
            await evo.send_text(number, "Nenhum lead encontrado.")
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

        logger.info("ADMIN_LEADS_SEND: number=%s", number)
        await evo.send_text(number, msg[:3500])
        return {"ok": True}

    # ========================================
    # 🤖 Regras normais do bot
    # ========================================
    reply = reply_for(number, text, state)
    logger.info("RULES_REPLY: number=%s reply=%r step=%s", number, reply, (state or {}).get("step"))

    if reply is None:
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
                instance=instance,
                from_number=number,
                nome=nome,
                telefone=telefone,
                assunto=assunto,
            )

            state["lead_saved"] = True
            logger.info("LEAD_SAVED: instance=%s number=%s", instance, number)
    except Exception as e:
        logger.error("LEAD_SAVE_ERROR: %s", e)

    # ========================================
    # 📤 Envio de resposta
    # ========================================
    logger.info("SEND_TEXT: to=%s chars=%s", number, len(reply or ""))
    
    try:
        await evo.send_text(number, reply)
        return {"ok": True, "sent": True}
    except Exception as e:
        logger.error("SEND_TEXT_ERROR: %s", e)
        return {"ok": True, "sent": False}

    
