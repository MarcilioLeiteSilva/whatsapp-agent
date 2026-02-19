import os
import logging
from fastapi import FastAPI, Request

from .evolution import EvolutionClient
from .store import MemoryStore
from .rules import reply_for, detect_intents
from .lead_logger import ensure_first_contact, mark_intent, save_handoff_lead, get_last_leads


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent")

ADMIN_NUMBER = os.getenv("ADMIN_NUMBER", "").strip()

app = FastAPI()
evo = EvolutionClient()
store = MemoryStore()


def extract_payload(payload: dict):
    instance = payload.get("instance")
    d = payload.get("data", payload)

    message_id = d.get("key", {}).get("id") or ""
    remote = d.get("key", {}).get("remoteJid") or ""
    from_number = remote.replace("@s.whatsapp.net", "")

    msg = d.get("message", {}) or {}
    text = msg.get("conversation") or ""

    from_me = bool(d.get("key", {}).get("fromMe"))
    is_group = remote.endswith("@g.us")
    return instance, message_id, from_number, text, from_me, is_group


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/webhook")
async def webhook(req: Request):
    payload = await req.json()
    logger.info("WEBHOOK: %s", payload)

    instance, message_id, number, text, from_me, is_group = extract_payload(payload)

    if from_me or is_group:
        return {"ok": True}

    if store.seen(message_id):
        return {"ok": True}

    if not number or not text:
        return {"ok": True}

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

        await evo.send_text(number, msg[:3500])
        return {"ok": True}

    # ========================================
    # 🤖 Regras normais do bot
    # ========================================
    reply = reply_for(number, text, state)

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

            # salva no Postgres (e faz backup CSV se habilitado)
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

    await evo.send_text(number, reply)
    return {"ok": True}
