from datetime import datetime
import re
import json
import logging
from zoneinfo import ZoneInfo
from . import ai_service
from .store import MemoryStore

store = MemoryStore()
logger = logging.getLogger("agent")
TZ = ZoneInfo("America/Sao_Paulo")

# =========================================================
# 🛠️ UTILITÁRIOS E PARSERS
# =========================================================

def normalize(t: str) -> str:
    return (t or "").lower().strip()

def parse_confirmation(text: str) -> bool:
    t = normalize(text)
    affirmative = ("sim", "vamos", "ok", "pode", "estou pronto", "bora", "claro", "1", "tá", "ta", "com certeza")
    return any(word in t for word in affirmative) or t == "1"

def parse_negative(text: str) -> bool:
    t = normalize(text)
    negative = ("não", "nao", "agora não", "2", "pare", "cancelar")
    return any(word in t for word in negative) or t == "2"

# =========================================================
# 📦 HANDLERS DE ETAPA (Workflow Engine)
# =========================================================

async def handle_inventory_pending(text: str, state: dict, number: str) -> str:
    """Etapa: Aguardando o usuário aceitar iniciar o acerto."""
    if parse_confirmation(text):
        items = state.get("inventory_items", [])
        if not items:
            state.pop("step", None)
            return "Certo! No momento não identifiquei itens pendentes para acerto. Caso precise de algo, digite *atendente*."
        
        # Garante que o robô ACORDE (remove pausa)
        store.set_paused(number, 0)
        
        msg = "Excelente! 🚀 Aqui estão os itens e as quantidades que constam para o seu PDV:\n\n"
        for i in items:
            msg += f"📦 *{i['product_name']}*: {i['expected_quantity']} unidades\n"
        
        msg += "\n*Confirma estas quantidades ou houve alguma alteração?*\n(Pode enviar tudo de uma vez, ex: 'Tenho 5 de um e 2 do outro')"
        state["step"] = "inventory_collecting"
        return msg
    
    if parse_negative(text):
        state.clear()
        store.set_paused(number, 31536000) # 1 ano
        return "Entendido! Sem problemas. Quando puder fazer a conferência, é só me avisar. Até logo! 👋"

    return "Para começarmos o acerto, por favor confirme:\n\nDigite *1* para Sim ou *2* para Não."

async def handle_inventory_collecting(text: str, state: dict) -> str:
    """Etapa: Extraindo as quantidades enviadas pelo usuário."""
    items_to_check = state.get("inventory_items", [])
    
    if parse_confirmation(text):
        extracted_items = [{"lot_id": i["lot_id"], "remaining": i["expected_quantity"], "product_name": i["product_name"]} for i in items_to_check]
        state["step"] = "inventory_summary"
        state["inventory_data"] = {"items": extracted_items}
        
        summary = "Perfeito! Então confirmo os dados originais:\n\n"
        for item in extracted_items:
            summary += f"✅ *{item.get('product_name')}*: {item.get('remaining')} unidades\n"
        summary += "\nEstá correto? Digite *1* para Confirmar ou *2* para Corrigir."
        return summary

    prompt = (
        f"Instrução: O lojista está informando o estoque atual dos produtos. Extraia EXATAMENTE as quantidades que ele possui em mãos agora.\n"
        f"Mensagem do lojista: \"{text}\"\n\n"
        f"Produtos esperados para conferência:\n"
        + "\n".join([f"- {i['product_name']} (ID: {i['lot_id']})" for i in items_to_check]) + "\n\n"
        "REGRAS CRÍTICAS:\n"
        "1. Ignore números que pareçam ser o total esperado (ex: se ele tem 3 de 15, o valor é 3).\n"
        "2. Se ele não mencionar um produto, use o valor 'expected' original.\n"
        "3. Retorne APENAS o JSON no formato:\n"
        "[{\"lot_id\": \"id\", \"remaining\": quantidade, \"product_name\": \"nome\"}]\n"
    )
    
    ai_res = await ai_service.ai_extract_json(prompt=prompt)
    try:
        clean_json = re.sub(r'```json|```', '', ai_res or "[]").strip()
        extracted_items = json.loads(clean_json)
        if extracted_items:
            state["step"] = "inventory_summary"
            state["inventory_data"] = {"items": extracted_items}
            
            summary = "Entendido! Veja se as alterações estão corretas:\n\n"
            for item in extracted_items:
                summary += f"✅ *{item.get('product_name')}*: {item.get('remaining')} unidades\n"
            summary += "\nEstá correto? Digite *1* para Confirmar ou *2* para Corrigir."
            return summary
    except:
        pass

    return "Não consegui identificar as quantidades. Pode me informar quantos itens você tem?"

async def handle_inventory_summary(text: str, state: dict) -> str:
    """Etapa: Confirmando o resumo da extração."""
    if parse_confirmation(text):
        state["step"] = "inventory_completed"
        return "Este é um acerto parcial. Na data do fechamento faremos a conferência e o reabastecimento. Recebido! ✅ Acerto encerrado com sucesso. Muito obrigado pela colaboração! 👋"
    
    if parse_negative(text):
        state["step"] = "inventory_collecting"
        return "Ops, perdão! Pode enviar novamente as quantidades?"
    
    return "Confirma os dados? Digite *1* para Confirmar ou *2* para Corrigir."

# =========================================================
# 🚦 ROTEADOR PRINCIPAL
# =========================================================

async def reply_for(number: str, text: str, state: dict, agent: any = None) -> str | None:
    t = normalize(text)
    step = state.get("step")

    if t in ("cancelar", "sair", "voltar"):
        state.clear()
        return "Atendimento reiniciado. Digite *menu* para ver as opções."

    if step == "inventory_pending":
        return await handle_inventory_pending(text, state, number)
    if step == "inventory_collecting":
        return await handle_inventory_collecting(text, state)
    if step == "inventory_summary":
        return await handle_inventory_summary(text, state)

    # IA Fallback para mensagens genéricas
    agent_rules = getattr(agent, "rules_json", {}) if agent else {}
    return await ai_service.ai_fallback_reply(user_text=text, agent_rules=agent_rules)

def detect_intents(text: str) -> list[str]:
    return []
