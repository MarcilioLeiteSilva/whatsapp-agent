# app/ai_guard.py
from __future__ import annotations

import re
from typing import Any, Optional


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def _has_menu_shape(text: str) -> bool:
    """
    Detecta formato de menu/fluxo:
    - várias linhas
    - ou itens numerados
    """
    t = _norm(text)
    if not t:
        return False
    # itens numerados "1 -", "1)", "1."
    if re.search(r"(^|\n)\s*\d+\s*[-\)\.]\s*", t):
        return True
    # "Escolha uma opção" / "Digite o número"
    if any(k in t for k in ["escolha uma opção", "escolha uma opcao", "digite o número", "digite o numero"]):
        return True
    return False


def _looks_operational(text: str) -> bool:
    """
    Mensagens operacionais/curtas ou que contêm comandos do fluxo.
    """
    t = _norm(text)
    if not t:
        return True

    # curto demais => alta chance de IA responder "meta"
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
        "handoff",
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

    # Se o usuário está pedindo atendente => não mexer (fluxo sensível)
    if "atendente" in ut:
        return False, "user_requested_handoff"

    # Se o state indica handoff/captura => não mexer
    step = _norm((state or {}).get("step"))
    if step in {"handoff", "handoff_waiting_lead", "lead_capture", "lead_captured"}:
        return False, f"handoff_step:{step}"

    # Se a resposta base parece menu/operacional => não mexer
    if _has_menu_shape(bt):
        return False, "menu_shape_in_base_reply"

    if _looks_operational(bt):
        return False, "operational_base_reply"

    return True, "ok"
