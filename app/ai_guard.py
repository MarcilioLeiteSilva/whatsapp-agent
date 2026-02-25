# app/ai_guard.py
from __future__ import annotations

import re
from typing import Any, Optional


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def _has_menu_shape(text: str) -> bool:
    """
    Detecta formato de menu/fluxo.
    """
    t = _norm(text)
    if not t:
        return False
    if re.search(r"(^|\n)\s*\d+\s*[-\)\.]\s*", t):
        return True
    if any(k in t for k in ["escolha uma opção", "escolha uma opcao", "digite o número", "digite o numero"]):
        return True
    return False


def _looks_operational(text: str) -> bool:
    """
    Mensagens muito operacionais (flow) — não reescrever.
    """
    t = _norm(text)
    if not t:
        return True

    if len(t) <= 45:
        return True

    keywords = [
        "digite",
        "menu",
        "opção",
        "opcao",
        "clique",
        "escolha",
        "envie no formato",
        "nome - telefone - assunto",
        "nome-telefone-assunto",
        "para prosseguir",
        "para continuar",
        "para avançar",
        "para avancar",
        "aguarde",
        "encaminhar",
    ]
    return any(k in t for k in keywords)


def ai_should_run(
    *,
    user_text: str,
    base_reply: Optional[str],
    state: Optional[dict[str, Any]] = None,
    paused: bool = False,
) -> tuple[bool, str]:
    """
    Decide se IA (assistive rewrite) pode rodar.

    Retorna (allowed, reason).
    """
    if paused:
        return False, "paused"

    bt = _norm(base_reply)
    ut = _norm(user_text)

    if not bt:
        return False, "empty_base_reply"

    # Se o usuário pediu atendente => fluxo sensível
    if ut == "atendente" or "atendente" in ut:
        return False, "user_requested_handoff"

    # ---- ALINHADO AO SEU rules_engine.apply_rules() ----
    step = _norm((state or {}).get("step"))

    # steps reais do engine:
    # - handoff_collect (coletando nome/telefone/assunto)
    # - lead_captured (já coletou)
    if step in {"handoff_collect", "lead_captured"}:
        return False, f"handoff_step:{step}"

    # Se já tem lead sendo montado e ainda não salvou: não mexer
    lead = (state or {}).get("lead")
    if isinstance(lead, dict) and lead and not (state or {}).get("lead_saved"):
        return False, "lead_capturing"

    # menu/operacional => não mexer
    if _has_menu_shape(bt):
        return False, "menu_shape_in_base_reply"

    if _looks_operational(bt):
        return False, "operational_base_reply"

    return True, "ok"
