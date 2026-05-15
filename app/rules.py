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

# =========================================================
# 🛠️ PARSERS DETERMINÍSTICOS (Sem IA)
# =========================================================

def parse_confirmation(text: str) -> bool:
    """Retorna True se for uma confirmação positiva."""
    t = (text or "").lower().strip()
    positives = ["1", "sim", "ok", "pode", "vamos", "concordo", "bora", "claro", "beleza", "tá", "ta", "confirmar"]
    # Verifica se a palavra exata está no texto ou se o texto começa com ela
    return any(p == t or t.startswith(p + " ") for p in positives)

def parse_negative(text: str) -> bool:
    """Retorna True se for uma negação."""
    t = (text or "").lower().strip()
    negatives = ["2", "não", "nao", "agora não", "depois", "cancelar", "parar", "n"]
    return any(n == t or t.startswith(n + " ") for n in negatives)

def normalize(text: str) -> str:
    return (text or "").strip().lower()

def in_business_hours(now: datetime) -> bool:
    if now.weekday() not in BUSINESS_DAYS:
        return False
    t = now.time()
    return BUSINESS_START <= t <= BUSINESS_END

# =========================================================
# 📦 HANDLERS DE ETAPA (Workflow Engine)
# =========================================================

async def handle_inventory_pending(text: str, state: dict) -> str:
    """Etapa: Aguardando o usuário aceitar iniciar o acerto."""
    if parse_confirmation(text):
        items = state.get("inventory_items", [])
        if not items:
            state.pop("step", None)
            return "Certo! No momento não identifiquei itens pendentes para acerto. Caso precise de algo, digite *atendente*."
        
        msg = "Excelente! 🚀 Aqui estão os itens e as quantidades que constam para o seu PDV:\n\n"
        for i in items:
            msg += f"📦 *{i['product_name']}*: {i['expected_quantity']} unidades\n"
        
        msg += "\n*Confirma estas quantidades ou houve alguma alteração?*\n(Pode enviar tudo de uma vez, ex: 'Tenho 5 de um e 2 do outro')"
        state["step"] = "inventory_collecting"
        return msg
    
    if parse_negative(text):
        state.clear()
        return "Entendido! Sem problemas. Quando puder fazer a conferência, é só me avisar. Até logo! 👋"

    return "Para começarmos o acerto, por favor confirme:\n\nDigite *1* para Sim ou *2* para Não."

async def handle_inventory_collecting(text: str, state: dict) -> str:
    """Etapa: Extraindo as quantidades enviadas pelo usuário."""
    items_to_check = state.get("inventory_items", [])
    
    # Se o usuário apenas disser que "Está tudo certo" ou confirmar os dados da lista
    if parse_confirmation(text):
        extracted_items = [{"lot_id": i["lot_id"], "remaining": i["expected_quantity"], "product_name": i["product_name"]} for i in items_to_check]
        state["step"] = "inventory_summary"
        state["inventory_data"] = {"items": extracted_items}
        
        summary = "Perfeito! Então confirmo os dados originais:\n\n"
        for item in extracted_items:
            summary += f"✅ *{item.get('product_name')}*: {item.get('remaining')} unidades\n"
        summary += "\nEstá correto? Digite *1* para Confirmar ou *2* para Corrigir."
        return summary

    # Processamento via IA para extração de mudanças
    prompt = (
        f"O usuário enviou uma resposta sobre o estoque: \"{text}\"\n\n"
        f"Temos os seguintes itens pendentes:\n"
        + "\n".join([f"- {i['product_name']} (ID: {i['lot_id']})" for i in items_to_check]) + "\n\n"
        "Extraia as quantidades restantes de cada item. Retorne APENAS um JSON no formato:\n"
        "[{\"lot_id\": \"id\", \"remaining\": quantidade, \"product_name\": \"nome\"}, ...]\n"
        "Se não encontrar algum, ignore-o no JSON."
    )
    
    ai_res = await ai_service.ai_extract_json(prompt=prompt)
    try:
        clean_json = re.sub(r'```json|```', '', ai_res or "[]").strip()
        extracted_items = json.loads(clean_json)
        
        if isinstance(extracted_items, list) and len(extracted_items) > 0:
            state["step"] = "inventory_summary"
            state["inventory_data"] = {"items": extracted_items}
            
            summary = "Entendido! Veja se as alterações estão corretas:\n\n"
            for item in extracted_items:
                summary += f"✅ *{item.get('product_name')}*: {item.get('remaining')} unidades\n"
            
            summary += "\nEstá correto? Digite *1* para Confirmar ou *2* para Corrigir."
            return summary
    except Exception as e:
        logger.error(f"Erro no parser de IA: {e}")

    return "Não consegui identificar as quantidades. 😅 Pode me informar quantos itens você tem de cada produto listado acima? (Ex: Tenho 5 do item X)"

async def handle_inventory_summary(text: str, state: dict) -> str:
    """Etapa: Confirmando o resumo da extração."""
    if parse_confirmation(text):
        state["step"] = "inventory_completed"
        # Não damos clear aqui ainda para que o main.py possa ler o 'inventory_completed' e disparar o webhook
        return "Recebido! ✅ Acerto encerrado com sucesso. Muito obrigado pela colaboração! 👋"
    
    if parse_negative(text):
        state["step"] = "inventory_collecting"
        return "Ops, perdão! 😅 Pode enviar novamente as quantidades para eu corrigir?"
    
    return "Por favor, confirme se os dados estão corretos:\n\nDigite *1* para Confirmar ou *2* para Corrigir."

# =========================================================
# 🚦 ROTEADOR PRINCIPAL (State Router)
# =========================================================

async def reply_for(number: str, text: str, state: dict, agent: any = None) -> str | None:
    t = normalize(text)
    now = datetime.now(TZ)
    step = state.get("step")
    agent_rules = getattr(agent, "rules_json", {}) if agent else {}

    # 1. Comandos Globais de Interrupção
    if t in ("voltar", "reativar", "#on", "sair", "cancelar acerto"):
        state.clear()
        return "Bot resetado ✅ Agora digite *menu* para ver as opções gerais."

    # 2. Roteamento por Estado (Workflow Engine)
    if step == "inventory_pending":
        return await handle_inventory_pending(text, state)
    
    if step == "inventory_collecting":
        return await handle_inventory_collecting(text, state)
    
    if step == "inventory_summary":
        return await handle_inventory_summary(text, state)

    # 3. Fluxos Legados / Gerais (Fora de Acerto)
    if step == "collect_contact":
        # ... lógica de lead capture (encurtada para brevidade) ...
        return "Recebi seus dados! Um atendente entrará em contato."

    # 4. Tratamento de Fora do Horário (Somente se não estiver em fluxo ativo)
    if not in_business_hours(now) and not step:
        if ai_service.AI_ENABLED:
            return await ai_service.ai_fallback_reply(user_text=text, agent_rules=agent_rules)
        return "No momento estamos fora do horário (9h às 18h). Digite *atendente* se for urgente."

    # 5. Menu Inicial (Somente se não estiver em fluxo ativo)
    if not step and t in ("menu", "oi", "olá", "ola", "inicio", "start"):
        state["step"] = "menu"
        if ai_service.AI_ENABLED:
            return await ai_service.ai_fallback_reply(user_text=text, agent_rules=agent_rules)
        return "Oi! 😊 Como posso ajudar?\n1) Serviços\n2) Horário\n3) Atendente"

    # 6. Fallback Final (Somente se nada capturou e não houver step ativo)
    if not step and ai_service.AI_ENABLED:
        return await ai_service.ai_fallback_reply(user_text=text, agent_rules=agent_rules)

    return "Não entendi 😅 Digite *menu* para ver as opções ou *cancelar* para reiniciar."

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
