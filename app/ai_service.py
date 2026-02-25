# app/ai_service.py
from __future__ import annotations

import os
import json
import logging
from typing import Optional, Dict, Any

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
    # Puxa tom/branding do rules_json se existir
    if not isinstance(agent_rules, dict):
        return "Seja claro, educado e objetivo."
    branding = agent_rules.get("branding") or {}
    name = branding.get("name") or "Atendimento"
    return f"Você é o assistente do {name}. Seja educado, direto e útil."


async def _call_openai(messages: list[dict]) -> Optional[str]:
    if not OPENAI_API_KEY:
        return None

    # Chat Completions (simples, sem SDK)
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

    # default openai
    return await _call_openai(messages)


async def ai_assist_reply(*, user_text: str, base_reply: str, agent_rules: Optional[dict] = None) -> str:
    """
    Reescreve a resposta base do rules para ficar mais humana e clara.
    """
    if not AI_ENABLED or not _mode_enabled("assistive"):
        return base_reply

    style = _agent_style_hint(agent_rules)

    sys = (
        "Você melhora a qualidade de mensagens de atendimento no WhatsApp.\n"
        f"{style}\n"
        "Regras:\n"
        "- Não invente informações.\n"
        "- Não mude o sentido.\n"
        "- Seja curto (até 5 linhas).\n"
        "- Use português do Brasil.\n"
    )

    messages = [
        {"role": "system", "content": sys},
        {
            "role": "user",
            "content": f"Mensagem do cliente:\n{user_text}\n\nResposta base:\n{base_reply}\n\nReescreva melhor:",
        },
    ]

    ollama_prompt = sys + "\n\n" + messages[-1]["content"]

    out = await _ai_text(messages, ollama_prompt)
    return out.strip() if out else base_reply


async def ai_fallback_reply(*, user_text: str, agent_rules: Optional[dict] = None) -> Optional[str]:
    """
    Gera uma resposta quando o rules não sabe responder.
    Retorna None se IA estiver desativada/sem provider.
    """
    if not AI_ENABLED or not _mode_enabled("fallback"):
        return None

    style = _agent_style_hint(agent_rules)

    sys = (
        "Você é um atendente de WhatsApp. Ajude o cliente com base no contexto.\n"
        f"{style}\n"
        "Regras:\n"
        "- Seja direto e educado.\n"
        "- Se não tiver informação suficiente, peça 1 pergunta objetiva.\n"
        "- Sempre ofereça opção de falar com atendente humano digitando 'atendente'.\n"
        "- Máximo 6 linhas.\n"
    )

    messages = [
        {"role": "system", "content": sys},
        {"role": "user", "content": f"Cliente disse: {user_text}\nResponda agora:"},
    ]
    ollama_prompt = sys + "\n\n" + messages[-1]["content"]

    out = await _ai_text(messages, ollama_prompt)
    return out.strip() if out else None
