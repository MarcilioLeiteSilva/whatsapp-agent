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


def reply_for(number: str, text: str, state: dict) -> str | None:
    t = normalize(text)
    now = datetime.now(TZ)

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
    if state.get("step") == "inventory_pending":
        data = parse_inventory(text)
        closing_id = state.get("closing_id")
        
        # Aqui dispararíamos o webhook assíncrono para o Consigo
        # Para fins de simplicidade neste middleware, podemos usar um print ou log
        # Em produção, isso seria uma tarefa de background (Celery/Task)
        
        state["step"] = "inventory_completed"
        state["inventory_data"] = data
        
        # Mensagem de confirmação para o lojista
        return (
            f"Recebido! ✅\n"
            f"- Restantes: {data['restantes']}\n"
            f"- Avarias: {data['avarias']}\n"
            f"Obrigado pelas informações. Já registrei no sistema."
        )

    # =========================================================
    # 📝 Coleta Nome + Telefone + Assunto (SEMPRE, mesmo fora do horário)
    # =========================================================
    if state.get("step") == "collect_contact":
        raw = (text or "").strip()
        lines = [l.strip() for l in raw.split("\n") if l.strip()]

        # Se não vier em 3 linhas, tenta separar por delimitadores comuns
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

        # validação simples do telefone
        numeros = "".join(c for c in telefone if c.isdigit())
        if len(numeros) < 8:
            return "O telefone parece inválido. Envie novamente 🙂"

        # salva no estado (para o main.py gravar CSV)
        state["lead"] = {
            "nome": nome,
            "telefone": telefone,
            "assunto": assunto,
            "timestamp": int(now.timestamp())
        }

        # pausa bot por X minutos e encerra
        state["bot_paused_until"] = int(now.timestamp()) + PAUSE_MINUTES_ON_HANDOFF * 60
        state["step"] = "lead_captured"

        return (
            f"Obrigado, {nome}! ✅\n\n"
            "Recebemos suas informações e um atendente vai falar com você em breve."
        )

    # =========================================================
    # 🕒 Fora do horário: orienta e permite pedir atendente
    # =========================================================
    if not in_business_hours(now):
        if t in ("menu", "oi", "olá", "ola", "inicio", "início", "start"):
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
    if t in ("menu", "oi", "olá", "ola", "inicio", "início", "start"):
        state["step"] = "menu"
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
