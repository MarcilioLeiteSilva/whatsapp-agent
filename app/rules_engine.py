# app/rules_engine.py
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Optional
from datetime import datetime

from sqlalchemy import select
from .db import SessionLocal
from .models import Agent

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore


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


def _safe_format(template: str, **kwargs) -> str:
    """
    Formata placeholders sem quebrar se o template tiver chaves extras.
    """
    try:
        return template.format(**kwargs)
    except Exception:
        return template


def _now_minutes_in_tz(tzname: str) -> int:
    """
    Retorna minutos do dia (0..1439) no timezone informado.
    Fallback: UTC.
    """
    if ZoneInfo:
        try:
            now = datetime.now(ZoneInfo(tzname))
            return now.hour * 60 + now.minute
        except Exception:
            pass

    # fallback UTC
    now = datetime.utcnow()
    return now.hour * 60 + now.minute


def in_business_hours(rules: dict) -> bool:
    """
    Horário comercial com timezone correto.

    Compatível com o JSON atual:
      "hours": {"mode":"business","open":"08:00","close":"18:00"}

    Novo (opcional):
      "hours": {"timezone":"America/Sao_Paulo"}

    Se não existir hours ou mode != business => considera aberto.
    """
    hours = rules.get("hours") or {}
    mode = (hours.get("mode") or "").lower()
    if mode != "business":
        return True

    open_h = str(hours.get("open") or "08:00").strip()
    close_h = str(hours.get("close") or "18:00").strip()

    # timezone: prioridade = rules.hours.timezone > env APP_TIMEZONE > America/Sao_Paulo
    tzname = str(hours.get("timezone") or os.getenv("APP_TIMEZONE", "America/Sao_Paulo")).strip() or "America/Sao_Paulo"

    # agora em minutos no fuso certo
    now_min = _now_minutes_in_tz(tzname)

    # parse de "HH:MM"
    try:
        oh, om = open_h.split(":")
        ch, cm = close_h.split(":")
        open_min = int(oh) * 60 + int(om)
        close_min = int(ch) * 60 + int(cm)
    except Exception:
        # se configuraram errado, não derruba atendimento
        return True

    return open_min <= now_min <= close_min


def menu_reply(rules: dict) -> str:
    """
    Mantém o menu atual (baseado em menu.options).
    Se existir ui.menu.fallback_text (novo), usa como texto do menu.
    """
    ui = rules.get("ui") or {}
    if isinstance(ui, dict):
        ui_menu = ui.get("menu") or {}
        if isinstance(ui_menu, dict):
            fb = ui_menu.get("fallback_text")
            if isinstance(fb, str) and fb.strip():
                return fb.strip()

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
    """
    Mantém compatibilidade:
    - primeiro tenta menu.options (antigo)
    - depois tenta menu.map (novo para rowId/buttonId)
    """
    menu = rules.get("menu") or {}
    t = (text or "").strip()

    # 1) antigo: menu.options com key "1/2/3"
    opts = menu.get("options") or []
    tl = t.lower()
    for o in opts:
        k = str(o.get("key") or "").strip().lower()
        if k and tl == k:
            return o

    # 2) novo opcional: menu.map (ex.: "menu:orcamento" -> reply)
    mp = menu.get("map")
    if isinstance(mp, dict) and t in mp:
        return {"key": t, "reply": mp.get(t)}

    return None


def apply_rules(number: str, text: str, state: dict, rules: dict) -> Optional[str]:
    """
    Motor genérico:
    - horário comercial (se hours.mode=business)
    - keyword handoff
    - menu + opções
    - captura lead (se capture_lead habilitado)

    Alterações:
    - handoff_ok suporta placeholders: {nome}, {telefone}, {assunto}
    - menu.map opcional (compatível com selectedRowId/selectedButtonId)
    """
    t = (text or "").strip()

    # Handoff keyword (mesmo comportamento)
    handoff = rules.get("handoff") or {}
    handoff_kw = (handoff.get("keyword") or "atendente").strip().lower()
    if t.lower() == handoff_kw:
        state["step"] = "handoff_collect"
        return get_text(
            rules,
            "messages.handoff_prompt",
            "Perfeito! Para encaminhar para um atendente, envie:\n*Nome* - *Telefone* - *Assunto*",
        )

    # Horário comercial (timezone correto)
    if not in_business_hours(rules):
        if (state.get("step") or "") not in ("handoff_collect", "lead_captured"):
            return get_text(
                rules,
                "messages.off_hours",
                "Estamos fora do horário agora 🙂. Se quiser atendimento, digite *atendente*.",
            )

    # Comandos (mesmo comportamento)
    if t.lower() in ("menu", "voltar"):
        state["step"] = "menu"
        return menu_reply(rules)

    # Se está coletando lead pro handoff
    if state.get("step") == "handoff_collect":
        parts = [p.strip() for p in t.split("-")]
        if len(parts) >= 3:
            nome = (parts[0] or "").strip()
            telefone = (parts[1] or "").strip()
            assunto = "-".join(parts[2:]).strip()

            state["lead"] = {"nome": nome, "telefone": telefone, "assunto": assunto}
            state["step"] = "lead_captured"

            tpl = get_text(
                rules,
                "messages.handoff_ok",
                "Obrigado, {nome}! ✅ Recebemos suas informações e um atendente vai falar com você em breve.",
            )

            return _safe_format(
                tpl,
                nome=nome or "🙂",
                telefone=telefone or "",
                assunto=assunto or "",
            )

        return get_text(
            rules,
            "messages.handoff_retry",
            "Não consegui entender. Envie no formato:\n*Nome* - *Telefone* - *Assunto*",
        )

    # Menu option (antigo + novo map)
    opt = match_menu_option(rules, t)
    if opt:
        state["step"] = f"menu:{opt.get('key')}"

        reply = opt.get("reply") or opt.get("ask") or "Ok."

        # macros simples no map (compatível)
        if isinstance(reply, str):
            if reply == "__SHOW_MENU__":
                state["step"] = "menu"
                return menu_reply(rules)

            if reply == "__HANDOFF__":
                state["step"] = "handoff_collect"
                return get_text(
                    rules,
                    "messages.handoff_prompt",
                    "Perfeito! Para encaminhar para um atendente, envie:\n*Nome* - *Telefone* - *Assunto*",
                )

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
