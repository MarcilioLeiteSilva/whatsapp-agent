from datetime import datetime, time as dtime
import re
import json
import logging
from zoneinfo import ZoneInfo
from . import ai_service

logger = logging.getLogger("agent")
TZ = ZoneInfo("America/Sao_Paulo")

# Configurações de Horário
BUSINESS_DAYS = {0, 1, 2, 3, 4}
BUSINESS_START = dtime(9, 0)
BUSINESS_END = dtime(18, 0)

def in_business_hours(now: datetime) -> bool:
    if now.weekday() not in BUSINESS_DAYS:
        return False
    t = now.time()
    return BUSINESS_START <= t <= BUSINESS_END

def parse_inventory(text: str) -> dict:
    res = {"restantes": 0, "avarias": 0, "perdas": 0}
    nums = re.findall(r'\d+', text)
    if nums:
        res["restantes"] = int(nums[0])
    return res

async def reply_for(number: str, text: str, state: dict, agent: any = None) -> str | None:
    t = (text or "").lower().strip()
    now = datetime.now(TZ)
    agent_rules = getattr(agent, "rules_json", {}) if agent else {}

    # =========================================================
    # 📦 Fluxo de Inventário (Agente de Acertos)
    # =========================================================
    if state.get("step") in ("inventory_pending", "inventory_collecting"):
        items_to_check = state.get("inventory_items", [])
        
        # Se o usuário está apenas confirmando o início ("Sim", "1", "Ok")
        affirmative = ("sim", "vamos", "ok", "pode", "estou pronto", "bora", "claro", "beleza", "tá", "ta", "com certeza", "1")
        negative = ("não", "nao", "agora não", "2")
        
        if state.get("step") == "inventory_pending":
            if t in negative:
                state.clear()
                return "Entendido! Quando puder fazer a conferência, é só me avisar. 👋"
            
            if any(word in t for word in affirmative) or t == "1":
                if not items_to_check:
                    return "Certo! No momento não identifiquei itens pendentes para acerto."
                
                msg = "Excelente! 🚀 Aqui estão os itens que constam para o seu PDV:\n\n"
                for i in items_to_check:
                    msg += f"📦 *{i['product_name']}*\n"
                
                msg += "\n*O que você ainda tem em mãos destes produtos?*\n(Pode enviar tudo de uma vez, ex: 'Tenho 5 de um e 2 do outro')"
                state["step"] = "inventory_collecting"
                return msg

        # Processamento do Inventário (IA)
        if ai_service.AI_ENABLED:
            prompt = (
                f"O usuário enviou uma resposta sobre o estoque: \"{text}\"\n\n"
                f"Temos os seguintes itens pendentes:\n"
                + "\n".join([f"- {i['product_name']} (ID: {i['lot_id']})" for i in items_to_check]) + "\n\n"
                "Extraia as quantidades restantes de cada item. Retorne APENAS um JSON no formato:\n"
                "[{\"lot_id\": \"id\", \"remaining\": quantidade}, ...]\n"
            )
            
            ai_res = await ai_service.ai_extract_json(prompt=prompt)
            try:
                clean_json = re.sub(r'```json|```', '', ai_res or "[]").strip()
                extracted_items = json.loads(clean_json)
                if isinstance(extracted_items, list) and len(extracted_items) > 0:
                    state["step"] = "inventory_completed"
                    state["inventory_data"] = {"items": extracted_items}
                    return "Recebido! ✅ Já registrei as quantidades informadas no sistema. Muito obrigado pela colaboração!"
            except:
                pass

        # Fallback manual simples
        data = parse_inventory(text)
        if data["restantes"] > 0:
            state["step"] = "inventory_completed"
            state["inventory_data"] = {"items": [{"lot_id": items_to_check[0]["lot_id"], "remaining": data["restantes"]}]} if items_to_check else {}
            return "Recebido! ✅ Registrei as quantidades. Obrigado!"
        
        return "Não consegui identificar as quantidades. Pode me informar quantos itens você tem?"

    # =========================================================
    # 🕒 Regras de Horário e Atendimento Geral
    # =========================================================
    if not in_business_hours(now):
        if ai_service.AI_ENABLED:
            return await ai_service.ai_fallback_reply(user_text=text, agent_rules=agent_rules)
        return "Estamos fora do horário comercial (9h às 18h)."

    if ai_service.AI_ENABLED:
        return await ai_service.ai_fallback_reply(user_text=text, agent_rules=agent_rules)

    return "Oi! Como posso ajudar você hoje?"

# === Intent detection (para compatibilidade com main.py) ===
def detect_intents(text: str) -> list[str]:
    return []
