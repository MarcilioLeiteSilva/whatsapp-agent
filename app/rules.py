from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")

# Horário comercial (seg-sex, 09:00–18:00)
BUSINESS_DAYS = {0, 1, 2, 3, 4}  # 0=segunda ... 6=domingo
BUSINESS_START = dtime(9, 0)
BUSINESS_END = dtime(18, 0)

PAUSE_MINUTES_ON_HANDOFF = 120  # 2 horas


def parse_inventory(text: str) -> dict:
    """Extrai quantidades estruturadas de uma resposta de inventário."""
    res = {"restantes": 0, "avarias": 0, "perdas": 0}
    t = text.lower()
    
    # Busca por números
    nums = re.findall(r'\d+', t)
    
    if not nums:
        return res

    # Lógica simples: se encontrar "avariado/estragado" perto de um número
    m_avar = re.search(r'(\d+)\s*(?:avariado|avaria|estragado|quebrado)', t)
    if m_avar:
        res["avarias"] = int(m_avar.group(1))
    
    m_perda = re.search(r'(\d+)\s*(?:perda|perdi|sumiu|falta)', t)
    if m_perda:
        res["perdas"] = int(m_perda.group(1))

    # O primeiro número que não for avaria/perda costuma ser o restante
    for n in nums:
        val = int(n)
        if val != res["avarias"] and val != res["perdas"]:
            res["restantes"] = val
            break
    
    # Fallback se só tiver um número
    if len(nums) == 1:
        res["restantes"] = int(nums[0])

    return res


import re
import httpx
from .settings import CONSIGO_WEBHOOK_URL


def normalize(text: str) -> str:
    return (text or "").strip().lower()


def in_business_hours(now: datetime) -> bool:
    if now.weekday() not in BUSINESS_DAYS:
        return False
    t = now.time()
    return BUSINESS_START <= t <= BUSINESS_END


from . import ai_service
import json

async def reply_for(number: str, text: str, state: dict, agent: any = None) -> str | None:
    t = normalize(text)
    now = datetime.now(TZ)
    
    # Busca regras do agente (branding, etc)
    agent_rules = getattr(agent, "rules_json", {}) if agent else {}

    # =========================================================
    # 🔁 Comandos para reativar bot (sempre funcionam)
    # =========================================================
    if t == "voltar" or t == "reativar" or t == "#on":
        state.pop("bot_paused_until", None)
        state.pop("step", None)
        state.pop("lead", None)
        state.pop("lead_saved", None)
        return "Bot reativado ✅ Agora digite *menu* para continuar."

    # =========================================================
    # ⛔ Se bot estiver pausado (handoff humano)
    # =========================================================
    paused_until = int(state.get("bot_paused_until") or 0)
    if paused_until and int(now.timestamp()) < paused_until:
        return None

    # =========================================================
    # 📦 Fluxo de Inventário (Agente de Acertos)
    # =========================================================
    if state.get("step") in ("inventory_pending", "inventory_collecting"):
        items_to_check = state.get("inventory_items", [])
        
        # Se o usuário está apenas confirmando o início ("Sim", "Vamos", "Ok")
        affirmative = ("sim", "vamos", "ok", "pode", "estou pronto", "bora", "claro", "beleza", "tá", "ta", "com certeza")
        if state.get("step") == "inventory_pending" and any(word in t for word in affirmative):
            if not items_to_check:
                return "Certo! No momento não identifiquei itens pendentes para acerto. Caso tenha algo aí, pode me falar o nome e a quantidade."
            
            msg = "Excelente! 🚀 Aqui estão os itens que constam para o seu PDV:\n\n"
            for i in items_to_check:
                msg += f"📦 *{i['product_name']}*\n"
            
            msg += "\n*O que você ainda tem em mãos destes produtos?*\n(Pode enviar tudo de uma vez, ex: 'Tenho 5 de um e 2 do outro')"
            state["step"] = "inventory_collecting"
            return msg

        # Processamento do Inventário (IA ou Manual)
        if ai_service.AI_ENABLED:
            prompt = (
                f"O usuário enviou uma resposta sobre o estoque: \"{text}\"\n\n"
                f"Temos os seguintes itens pendentes:\n"
                + "\n".join([f"- {i['product_name']} (ID: {i['lot_id']})" for i in items_to_check]) + "\n\n"
                "Extraia as quantidades restantes de cada item. Retorne APENAS um JSON no formato:\n"
                "[{\"lot_id\": \"id\", \"remaining\": quantidade}, ...]\n"
                "Se não encontrar algum, ignore-o no JSON."
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

        # Fallback para parsing manual (legado/simples)
        data = parse_inventory(text)
        if data["restantes"] > 0 or data["avarias"] > 0 or data["perdas"] > 0:
            if len(items_to_check) == 1:
                state["step"] = "inventory_completed"
                state["inventory_data"] = {
                    "items": [{"lot_id": items_to_check[0]["lot_id"], "remaining": data["restantes"]}]
                }
                return f"Recebido! ✅ Registrei {data['restantes']} unidades de {items_to_check[0]['product_name']}. Obrigado!"
            
            state["step"] = "inventory_completed"
            state["inventory_data"] = data
            return "Recebido! ✅ Já registrei as informações. Obrigado!"
        
        # Se não entendeu nada e não foi um "Sim", pede para ser mais específico
        return "Não consegui identificar as quantidades. 😅 Pode me informar quantos itens você tem de cada produto listado acima?"

    # =========================================================
    # 📝 Coleta Nome + Telefone + Assunto (SEMPRE, mesmo fora do horário)
    # =========================================================
    if state.get("step") == "collect_contact":
        raw = (text or "").strip()
        lines = [l.strip() for l in raw.split("\n") if l.strip()]

        if len(lines) < 3:
            tmp = (
                raw.replace("|", "\n")
                   .replace(";", "\n")
                   .replace(",", "\n")
                   .replace(" - ", "\n")
                   .replace("-", "\n")
            )
            parts = [p.strip() for p in tmp.split("\n") if p.strip()]
            if len(parts) >= 3:
                lines = parts[:3]

        if len(lines) < 3:
            return (
                "Para eu encaminhar certinho, envie assim:\n\n"
                "✅ Exemplo 3 linhas:\n"
                "João Silva\n"
                "31999998888\n"
                "Transferência\n\n"
                "✅ Exemplo 1 linha:\n"
                "João Silva - 31999998888 - Transferência"
            )

        nome = lines[0]
        telefone = lines[1]
        assunto = lines[2]

        numeros = "".join(c for c in telefone if c.isdigit())
        if len(numeros) < 8:
            return "O telefone parece inválido. Envie novamente 🙂"

        state["lead"] = {
            "nome": nome,
            "telefone": telefone,
            "assunto": assunto,
            "timestamp": int(now.timestamp())
        }

        state["bot_paused_until"] = int(now.timestamp()) + PAUSE_MINUTES_ON_HANDOFF * 60
        state["step"] = "lead_captured"

        return (
            f"Obrigado, {nome}! ✅\n\n"
            "Recebemos suas informações e um atendente vai falar com você em breve."
        )

    # =========================================================
    # 🕒 Fora do horário
    # =========================================================
    if not in_business_hours(now):
        # Se IA habilitada, deixa ela responder de forma educada
        if not state.get("step") and ai_service.AI_ENABLED:
            ai_reply = await ai_service.ai_fallback_reply(user_text=text, agent_rules=agent_rules)
            if ai_reply: return ai_reply

        if not state.get("step") and t in ("menu", "oi", "olá", "ola", "inicio", "início", "start"):
            return (
                "Oi! 😊 No momento estamos *fora do horário* (seg-sex, 9h às 18h).\n\n"
                "Se quiser falar com um atendente, digite *atendente*."
            )

        if "atendente" in t or t == "3":
            state["step"] = "collect_contact"
            return (
                "Perfeito! Para te encaminhar para um atendente, envie:\n\n"
                "*Nome completo*\n"
                "*Telefone*\n"
                "*Assunto*\n\n"
                "Ou em 1 linha:\n"
                "João Silva - 31999998888 - Transferência"
            )

        return "Estamos fora do horário agora 🙂 Se quiser atendimento, digite *atendente*."

    # =========================================================
    # 📋 Menu principal (dentro do horário)
    # =========================================================
    if not state.get("step") and t in ("menu", "oi", "olá", "ola", "inicio", "início", "start"):
        state["step"] = "menu"
        # Se IA habilitada, deixa ela dar as boas vindas
        if ai_service.AI_ENABLED:
            ai_reply = await ai_service.ai_fallback_reply(user_text=text, agent_rules=agent_rules)
            if ai_reply: return ai_reply
            
        return (
            "Oi! 😊 Sou o atendimento automático.\n\n"
            "Digite uma opção:\n"
            "1) Serviços\n"
            "2) Horário\n"
            "3) Atendente"
        )

    if t == "1":
        state["step"] = "services"
        return (
            "Qual serviço você precisa?\n"
            "A) Placas\n"
            "B) Transferência\n"
            "C) Regularização\n\n"
            "Responda com A, B ou C."
        )

    if t == "2":
        return "Atendemos de seg a sex, 9h às 18h."

    if t == "3" or "atendente" in t:
        state["step"] = "collect_contact"
        return (
            "Perfeito! Para te encaminhar para um atendente, envie:\n\n"
            "*Nome completo*\n"
            "*Telefone*\n"
            "*Assunto*\n\n"
            "Ou em 1 linha:\n"
            "João Silva - 31999998888 - Transferência"
        )

    # =========================================================
    # 🔧 Serviços (fluxo simples A/B/C)
    # =========================================================
    if state.get("step") == "services":
        if t in ("a", "b", "c"):
            state["service_choice"] = t
            state["step"] = "done"
            return (
                f"Perfeito! Você escolheu {t.upper()}.\n"
                "Um atendente continuará com você."
            )
        return "Responda com A, B ou C 🙂"

    # =========================================================
    # 🤖 FALLBACK IA (Se nada acima capturou)
    # =========================================================
    if ai_service.AI_ENABLED:
        ai_reply = await ai_service.ai_fallback_reply(user_text=text, agent_rules=agent_rules)
        if ai_reply:
            return ai_reply

    return "Não entendi 😅 Digite *menu* para ver as opções."


# === Intent detection (captura automática - produto) ===
INTENT_KEYWORDS = {
    "orcamento": ["orçamento", "orcamento", "cotação", "cotacao"],
    "preco": ["preço", "preco", "valor", "quanto custa", "quanto fica"],
    "transferencia": ["transferência", "transferencia", "transferir"],
    "documento": ["documento", "documentos", "crlv", "licenciamento"],
    "atendente": ["atendente", "humano", "falar com", "suporte"],
}

def detect_intents(text: str) -> list[str]:
    t = (text or "").lower().strip()
    found: list[str] = []
    for key, kws in INTENT_KEYWORDS.items():
        for kw in kws:
            if kw in t:
                found.append(key)
                break
    return found
