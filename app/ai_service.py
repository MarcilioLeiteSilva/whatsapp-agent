# app/ai_service.py
from __future__ import annotations

import os
import re
import logging
from typing import Optional

import httpx

logger = logging.getLogger("agent")

AI_ENABLED = os.getenv("AI_ENABLED", "false").strip().lower() in ("1", "true", "yes", "y")
AI_PROVIDER = (os.getenv("AI_PROVIDER", "openai") or "openai").strip().lower()

AI_MODE = (os.getenv("AI_MODE", "assistive") or "assistive").strip().lower()
AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "220"))
AI_TIMEOUT = float(os.getenv("AI_TIMEOUT", "10"))

# OpenAI
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY", "") or "").strip()
OPENAI_MODEL = (os.getenv("OPENAI_MODEL", "gpt-4o-mini") or "gpt-4o-mini").strip()

# Ollama
OLLAMA_BASE_URL = (os.getenv("OLLAMA_BASE_URL", "http://ollama:11434") or "").strip().rstrip("/")
OLLAMA_MODEL = (os.getenv("OLLAMA_MODEL", "qwen2.5:3b") or "qwen2.5:3b").strip()


def _mode_enabled(name: str) -> bool:
    # AI_MODE pode ser: "assistive" ou "fallback" ou "assistive,fallback"
    parts = {p.strip() for p in AI_MODE.split(",") if p.strip()}
    return name in parts


def _agent_style_hint(agent_rules: Optional[dict]) -> str:
    if not isinstance(agent_rules, dict):
        return "Seja claro, educado e objetivo."
    branding = agent_rules.get("branding") or {}
    name = branding.get("name") or "Atendimento"
    return f"Você é o assistente do {name}. Seja educado, direto e útil."


def _should_bypass_assistive(base_reply: str) -> bool:
    """
    Evita IA em mensagens operacionais/de fluxo (menus, instruções curtas),
    onde o modelo costuma "narrar" ou alterar a intenção do fluxo.
    """
    s = (base_reply or "").strip().lower()
    if not s:
        return True

    # mensagens típicas de fluxo
    keywords = [
        "digite atendente",
        "digite menu",
        "escolha uma opção",
        "escolha uma opcao",
        "menu ",
        "1 -",
        "2 -",
        "3 -",
        "4 -",
        "5 -",
        "opção desejada",
        "opcao desejada",
    ]
    if any(k in s for k in keywords):
        return True

    # muito curta => costuma virar "meta"
    if len(s) <= 40:
        return True

    return False


def _strip_meta_prefix(text: str) -> str:
    """
    Remove frases meta como "Claro! Aqui está..." e aspas em volta.
    """
    if not text:
        return text

    t = text.strip()

    # remove aspas envolvendo tudo
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("“") and t.endswith("”")):
        t = t[1:-1].strip()

    # remove prefixos meta comuns (primeira linha)
    bad_starts = [
        "claro! aqui está",
        "claro! aqui esta",
        "aqui está uma versão",
        "aqui esta uma versao",
        "aqui está a versão",
        "aqui esta a versao",
        "segue uma versão",
        "segue uma versao",
        "versão melhorada",
        "versao melhorada",
        "mensagem melhorada",
        "resposta melhorada",
    ]
    low = t.lower()
    if any(low.startswith(p) for p in bad_starts):
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        if len(lines) > 1:
            t = "\n".join(lines[1:]).strip()

    # se o modelo colocou algo tipo: "Texto:" ou "Resposta:"
    t = re.sub(r"^(texto|resposta)\s*:\s*", "", t.strip(), flags=re.IGNORECASE)

    return t.strip()


async def _call_openai(messages: list[dict]) -> Optional[str]:
    if not OPENAI_API_KEY:
        return None

    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "max_tokens": AI_MAX_TOKENS,
        "temperature": 0.4,
    }

    try:
        async with httpx.AsyncClient(timeout=AI_TIMEOUT) as c:
            r = await c.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", None)
    except Exception as e:
        logger.warning("AI_OPENAI_ERROR: %s", e)
        return None


async def _call_ollama(prompt: str) -> Optional[str]:
    if not OLLAMA_BASE_URL:
        return None

    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.4},
    }

    try:
        async with httpx.AsyncClient(timeout=AI_TIMEOUT) as c:
            r = await c.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
        return (data.get("response") or "").strip() or None
    except Exception as e:
        logger.warning("AI_OLLAMA_ERROR: %s", e)
        return None


async def _ai_text(messages: list[dict], ollama_prompt: str) -> Optional[str]:
    if not AI_ENABLED:
        return None

    if AI_PROVIDER == "ollama":
        return await _call_ollama(ollama_prompt)

    return await _call_openai(messages)


async def ai_assist_reply(*, user_text: str, base_reply: str, agent_rules: Optional[dict] = None) -> str:
    """
    Reescreve a resposta base do rules para ficar mais humana e clara,
    SEM nunca adicionar frases meta (ex: "Aqui está uma versão melhorada").
    """
    if not AI_ENABLED or not _mode_enabled("assistive"):
        return base_reply

    # BYPASS para mensagens de fluxo/operacionais
    if _should_bypass_assistive(base_reply):
        return base_reply

    style = _agent_style_hint(agent_rules)

    sys = (
        "Você é um revisor de mensagens para atendimento via WhatsApp.\n"
        f"{style}\n"
        "Regras OBRIGATÓRIAS:\n"
        "- Retorne APENAS o texto final que será enviado ao cliente.\n"
        "- NÃO explique o que você fez.\n"
        "- NÃO inclua prefácios como 'Aqui está uma versão melhorada', 'Claro!' etc.\n"
        "- NÃO use aspas.\n"
        "- Não invente informações.\n"
        "- Não mude o sentido.\n"
        "- Seja curto (até 5 linhas).\n"
        "- Português do Brasil.\n"
    )

    user_prompt = (
        "Reescreva a resposta BASE para enviar ao cliente.\n"
        "Retorne somente a mensagem final.\n\n"
        f"Cliente: {user_text}\n\n"
        f"BASE: {base_reply}\n"
    )

    messages = [
        {"role": "system", "content": sys},
        {"role": "user", "content": user_prompt},
    ]

    ollama_prompt = sys + "\n\n" + user_prompt

    out = await _ai_text(messages, ollama_prompt)
    if not out:
        return base_reply

    out = _strip_meta_prefix(out)

    # Se por algum motivo ficou vazio, não quebra o fluxo
    return out if out else base_reply


async def ai_fallback_reply(*, user_text: str, agent_rules: Optional[dict] = None) -> Optional[str]:
    """
    Gera uma resposta quando o rules não sabe responder.
    """
    if not AI_ENABLED or not _mode_enabled("fallback"):
        return None

    style = _agent_style_hint(agent_rules)

    sys = (
        "Você é um atendente de WhatsApp.\n"
        f"{style}\n"
        "Regras OBRIGATÓRIAS:\n"
        "- Retorne APENAS a mensagem final para o cliente.\n"
        "- NÃO explique sua resposta.\n"
        "- NÃO use aspas.\n"
        "- Seja direto e educado.\n"
        "- Se não tiver informação suficiente, faça 1 pergunta objetiva.\n"
        "- Sempre ofereça opção de falar com atendente humano digitando 'atendente'.\n"
        "- Máximo 6 linhas.\n"
    )

    user_prompt = f"Cliente disse: {user_text}\nResponda agora:"

    messages = [
        {"role": "system", "content": sys},
        {"role": "user", "content": user_prompt},
    ]
    ollama_prompt = sys + "\n\n" + user_prompt

    out = await _ai_text(messages, ollama_prompt)
    if not out:
        return None

    out = _strip_meta_prefix(out)
    return out if out else None
