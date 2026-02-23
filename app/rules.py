"""
app/rules.py

Regras do bot (modo SaaS multiagente):

- Agora o menu/regras são carregados POR AGENTE via agents.rules_json (Postgres JSONB).
- O main.py e o ChatLab devem chamar reply_for(..., ctx={client_id, agent_id, instance})
  para que o motor consiga carregar as regras corretas.
- Mantém detect_intents() simples (lead quente) e utilidades.

Dependências:
- app/rules_engine.py (motor genérico + cache)
"""

from __future__ import annotations

import re
from typing import Optional, Dict, Any, List

from .rules_engine import load_rules_for_agent, apply_rules


# -----------------------------------------------------------------------------
# Intents (lead quente) — você pode expandir isso depois
# -----------------------------------------------------------------------------
_INTENT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("orcamento", re.compile(r"\b(orçamento|orcamento|valor|preço|preco|quanto custa)\b", re.I)),
    ("comprar", re.compile(r"\b(comprar|quero|preciso|contratar|fechar)\b", re.I)),
    ("urgente", re.compile(r"\b(urgente|pra hoje|agora|imediato)\b", re.I)),
    ("atendente", re.compile(r"\b(atendente|humano|pessoa|suporte)\b", re.I)),
    ("transferencia", re.compile(r"\b(transfer(ê|e)ncia|transferir)\b", re.I)),
    ("placas", re.compile(r"\b(placa|placas|segunda via|2a via|extravio)\b", re.I)),
]


def detect_intents(text: str) -> List[str]:
    """
    Retorna lista de intents detectadas a partir do texto.
    (Usado para marcar lead quente no lead_logger.)
    """
    t = (text or "").strip()
    if not t:
        return []
    hits: list[str] = []
    for name, pat in _INTENT_PATTERNS:
        if pat.search(t):
            hits.append(name)
    # remove duplicados preservando ordem
    seen = set()
    out = []
    for x in hits:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


# -----------------------------------------------------------------------------
# Core: reply_for (agora recebe ctx)
# -----------------------------------------------------------------------------
def reply_for(number: str, text: str, state: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Gera resposta do bot.

    Parâmetros:
      - number: número do usuário (E.164 sem +)
      - text: mensagem recebida
      - state: dict mutável com estado da conversa (MemoryStore)
      - ctx: contexto do roteamento SaaS:
            {
              "client_id": "...",
              "agent_id": "...",
              "instance": "agente001"
            }

    Retorno:
      - str: resposta do bot
      - None: interpretado como "pausado/handoff" pelo main.py (não enviar mensagem)
    """
    ctx = ctx or {}
    agent_id = (ctx.get("agent_id") or "").strip()

    # Carrega rules_json do banco por agent_id (com cache curto no engine)
    rules = load_rules_for_agent(agent_id) if agent_id else {}

    # Delegação para motor genérico
    return apply_rules(number, text, state, rules)
