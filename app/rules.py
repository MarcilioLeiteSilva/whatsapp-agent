# app/rules.py
"""
app/rules.py

Regras do bot (modo SaaS multiagente):

- menu/regras carregados POR AGENTE via agents.rules_json (Postgres JSONB).
- main.py e ChatLab devem chamar reply_for(..., ctx={client_id, agent_id, instance})
- Mantém detect_intents() e utilidades.

Dependências:
- app/rules_engine.py
"""

from __future__ import annotations

import re
from typing import Optional, Dict, Any, List

from .rules_engine import load_rules_for_agent, apply_rules


# ---------------------------------------------------------------------
# Intents universais (cross-nicho)
# ---------------------------------------------------------------------
_INTENT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("orcamento", re.compile(r"\b(orçamento|orcamento|valor|preço|preco|quanto custa|cotação|cotacao)\b", re.I)),
    ("comprar", re.compile(r"\b(comprar|quero|preciso|contratar|fechar|assin(ar|atura)|matricular)\b", re.I)),
    ("agendamento", re.compile(r"\b(agendar|agendamento|marcar horário|marcar horario|consulta|visita)\b", re.I)),
    ("horario_funcionamento", re.compile(r"\b(hor[aá]rio|funciona|aberto|fecha|sábado|sabado|domingo)\b", re.I)),
    ("endereco", re.compile(r"\b(endere[cç]o|localiza[cç][aã]o|onde fica|como chegar|maps|google)\b", re.I)),
    ("suporte", re.compile(r"\b(suporte|ajuda|problema|erro|não funciona|nao funciona|bug)\b", re.I)),
    ("pedido_status", re.compile(r"\b(pedido|status|andamento|entrega|prazo|rastrear|rastreamento)\b", re.I)),
    ("financeiro", re.compile(r"\b(boleto|pix|pagamento|nota fiscal|nf|cobran[cç]a|reembolso|estorno)\b", re.I)),
    ("cancelamento", re.compile(r"\b(cancelar|cancelamento|desistir|reclama[cç][aã]o)\b", re.I)),
    ("urgente", re.compile(r"\b(urgente|pra hoje|agora|imediato)\b", re.I)),
    ("atendente", re.compile(r"\b(atendente|humano|pessoa|falar com alguém|falar com alguem)\b", re.I)),
    ("transferencia", re.compile(r"\b(transfer(ê|e)ncia|transferir|setor|departamento)\b", re.I)),
]


def detect_intents(text: str, rules: Optional[dict] = None) -> List[str]:
    """
    Detecta intents a partir do texto.
    Suporta intents custom por agente via rules_json:

      rules["intents"]["custom"] = [
        {"name": "placas", "patterns": ["placa", "segunda via", "extravio"]},
        {"name": "viagem", "patterns": ["passagem", "hotel", "pacote"]},
      ]
    """
    t = (text or "").strip()
    if not t:
        return []

    hits: list[str] = []

    # fixas
    for name, pat in _INTENT_PATTERNS:
        if pat.search(t):
            hits.append(name)

    # custom por agente (sem regex obrigatória; aceita texto simples também)
    try:
        if isinstance(rules, dict):
            intents_cfg = rules.get("intents") or {}
            custom = intents_cfg.get("custom") or []
            if isinstance(custom, list):
                for item in custom:
                    if not isinstance(item, dict):
                        continue
                    nm = (item.get("name") or "").strip()
                    pats = item.get("patterns") or []
                    if not nm or not isinstance(pats, list):
                        continue
                    for p in pats:
                        ps = str(p or "").strip()
                        if not ps:
                            continue
                        # se o pattern parece regex, a pessoa pode usar; senão funciona como substring também
                        try:
                            if re.search(ps, t, re.I):
                                hits.append(nm)
                                break
                        except re.error:
                            if ps.lower() in t.lower():
                                hits.append(nm)
                                break
    except Exception:
        pass

    # de-dupe preservando ordem
    seen = set()
    out: list[str] = []
    for x in hits:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def reply_for(number: str, text: str, state: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Gera resposta do bot.
    Retorna None => "pausado/handoff" (main.py não envia)
    """
    ctx = ctx or {}
    agent_id = (ctx.get("agent_id") or "").strip()

    rules = load_rules_for_agent(agent_id) if agent_id else {}
    return apply_rules(number, text, state, rules)
