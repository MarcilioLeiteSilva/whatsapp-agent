# app/rules_engine.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from .db import SessionLocal
from .models import Agent


@dataclass
class AgentRules:
    agent_id: str
    client_id: str
    instance: str
    rules: dict


# cache simples por agent_id (DEV ok)
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_SECONDS = 0  # 0 = sempre reflete mudanças (sem TTL)

def _now() -> float:
    return time.time()


def load_rules_for_agent(agent_id: str) -> dict:
    if not agent_id:
        return {}

    hit = _CACHE.get(agent_id)
    if hit:
        # TTL opcional (aqui 0, mas mantemos estrutura)
        ts, rules = hit
        if _CACHE_TTL_SECONDS <= 0 or (_now() - ts) <= _CACHE_TTL_SECONDS:
            return rules

    with SessionLocal() as db:
        a = db.execute(select(Agent).where(Agent.id == agent_id).limit(1)).scalar_one_or_none()
        rules = (getattr(a, "rules_json", None) or {}) if a else {}

    _CACHE[agent_id] = (_now(), rules)
    return rules


def invalidate_agent_rules(agent_id: str) -> None:
    _CACHE.pop(agent_id, None)


def get_text(d: dict, path: str, default: str = "") -> str:
    """
    Helper para ler chaves do JSON: "messages.welcome"
    """
    cur: Any = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur if isinstance(cur, str) else default


def in_business_hours(rules: dict) -> bool:
    """
    Simples: se não existir hours, considera aberto.
    (Depois evoluímos para timezone real e janelas por dia)
    """
    hours = rules.get("hours") or {}
    mode = (hours.get("mode") or "").lower()
    if mode != "business":
        return True

    open_h = hours.get("open") or "08:00"
    close_h = hours.get("close") or "18:00"

    # usa hora do sistema do container (depois: tz)
    lt = time.localtime()
    now_min = lt.tm_hour * 60 + lt.tm_min

    oh, om = open_h.split(":")
    ch, cm = close_h.split(":")
    open_min = int(oh) * 60 + int(om)
    close_min = int(ch) * 60 + int(cm)

    return open_min <= now_min <= close_min


def menu_reply(rules: dict) -> str:
    menu = rules.get("menu") or {}
    title = menu.get("title") or "Menu"
    opts = menu.get("options") or []
    lines = [f"*{title}*"]
    for o in opts:
        k = str(o.get("key") or "").strip()
        label = str(o.get("label") or "").strip()
        if k and label:
            lines.append(f"{k} - {label}")
    lines.append("\nDigite o número da opção ou *menu* para ver novamente.")
    return "\n".join(lines)


def match_menu_option(rules: dict, text: str) -> Optional[dict]:
    menu = rules.get("menu") or {}
    opts = menu.get("options") or []
    t = (text or "").strip().lower()
    for o in opts:
        k = str(o.get("key") or "").strip().lower()
        if k and t == k:
            return o
    return None


def apply_rules(number: str, text: str, state: dict, rules: dict) -> Optional[str]:
    """
    Motor genérico:
    - horário comercial (se hours.mode=business)
    - keyword handoff
    - menu + opções
    - captura lead (se capture_lead habilitado)
    """
    t = (text or "").strip()

    # Handoff keyword
    handoff = rules.get("handoff") or {}
    handoff_kw = (handoff.get("keyword") or "atendente").strip().lower()
    if t.lower() == handoff_kw:
        state["step"] = "handoff_collect"
        return get_text(
            rules,
            "messages.handoff_prompt",
            "Perfeito! Para encaminhar para um atendente, envie:\n*Nome* - *Telefone* - *Assunto*",
        )

    # Horário comercial
    if not in_business_hours(rules):
        if (state.get("step") or "") not in ("handoff_collect", "lead_captured"):
            return get_text(
                rules,
                "messages.off_hours",
                "Estamos fora do horário agora 🙂. Se quiser atendimento, digite *atendente*.",
            )

    # Comandos
    if t.lower() in ("menu", "voltar"):
        state["step"] = "menu"
        return menu_reply(rules)

    # Se está coletando lead pro handoff
    if state.get("step") == "handoff_collect":
        parts = [p.strip() for p in t.split("-")]
        if len(parts) >= 3:
            nome = parts[0]
            telefone = parts[1]
            assunto = "-".join(parts[2:]).strip()
            state["lead"] = {"nome": nome, "telefone": telefone, "assunto": assunto}
            state["step"] = "lead_captured"
            return get_text(
                rules,
                "messages.handoff_ok",
                "Obrigado! ✅ Recebemos suas informações e um atendente vai falar com você em breve.",
            )

        return get_text(
            rules,
            "messages.handoff_retry",
            "Não consegui entender. Envie no formato:\n*Nome* - *Telefone* - *Assunto*",
        )

    # Menu option
    opt = match_menu_option(rules, t)
    if opt:
        state["step"] = f"menu:{opt.get('key')}"
        reply = opt.get("reply") or opt.get("ask") or "Ok."
        return str(reply)

    # Default / welcome
    if not state.get("step"):
        state["step"] = "welcome"
        return get_text(
            rules,
            "messages.welcome",
            "Olá! Digite *menu* para ver opções.",
        )

    # fallback
    return get_text(
        rules,
        "messages.fallback",
        "Não entendi. Digite *menu* para ver opções.",
    )
