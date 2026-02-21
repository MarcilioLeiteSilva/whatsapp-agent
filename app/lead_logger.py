import os
import csv
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import Lead, Agent

logger = logging.getLogger("agent")

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------
DEFAULT_CLIENT_ID = os.getenv("CLIENT_ID", "").strip()  # compat/legacy
LEADS_CSV_PATH = os.getenv("LEADS_CSV_PATH", "/opt/whatsapp-agent/leads.csv").strip()
ENABLE_CSV_BACKUP = os.getenv("ENABLE_CSV_BACKUP", "1").strip() not in ("0", "false", "False", "")


def _now_utc():
    return datetime.now(timezone.utc)


def _safe_str(x) -> str:
    return "" if x is None else str(x)


# -------------------------------------------------------------------
# Agents (multi-tenant resolver)
# -------------------------------------------------------------------
def get_agent_by_instance(instance: str) -> Optional[Agent]:
    if not instance:
        return None
    with SessionLocal() as db:
        return db.execute(select(Agent).where(Agent.instance == instance)).scalar_one_or_none()


# -------------------------------------------------------------------
# Internal helpers
# -------------------------------------------------------------------
def _find_lead(db: Session, *, client_id: str, agent_id: Optional[str], from_number: str) -> Optional[Lead]:
    """
    Encontra o lead "corrente" para um contato.
    Critério multi-agente: (client_id, agent_id, from_number).
    Se agent_id vier vazio/None, cai em compatibilidade (client_id + from_number).
    """
    q = select(Lead).where(Lead.client_id == client_id, Lead.from_number == from_number)

    # Se tiver agent_id, restringe por agente (multi-número por cliente).
    if agent_id:
        q = q.where(Lead.agent_id == agent_id)

    return db.execute(q.order_by(Lead.created_at.desc())).scalars().first()


def _ensure_csv_header(path: str):
    if not ENABLE_CSV_BACKUP:
        return
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["created_at", "client_id", "agent_id", "instance", "from_number", "nome", "telefone", "assunto"])


def _append_csv(path: str, row: dict):
    if not ENABLE_CSV_BACKUP:
        return
    try:
        _ensure_csv_header(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                _safe_str(row.get("created_at")),
                _safe_str(row.get("client_id")),
                _safe_str(row.get("agent_id")),
                _safe_str(row.get("instance")),
                _safe_str(row.get("from_number")),
                _safe_str(row.get("nome")),
                _safe_str(row.get("telefone")),
                _safe_str(row.get("assunto")),
            ])
    except Exception as e:
        logger.error("CSV_BACKUP_ERROR: %s", e)


# -------------------------------------------------------------------
# Public API used by main.py
# -------------------------------------------------------------------
def ensure_first_contact(
    *,
    client_id: Optional[str],
    agent_id: Optional[str],
    instance: Optional[str],
    from_number: str,
    origem: str = "whatsapp",
) -> None:
    """
    Garante que exista um registro de lead para o primeiro contato.
    - Se não existe: cria com first_seen_at.
    - Se existe: apenas atualiza updated_at (sem duplicar).
    """
    cid = (client_id or DEFAULT_CLIENT_ID or "").strip()
    if not cid:
        raise RuntimeError("client_id vazio (defina CLIENT_ID para legado ou passe client_id dinamicamente).")

    inst = (instance or "").strip()
    aid = (agent_id or "").strip() or None
    num = (from_number or "").strip()

    if not num:
        return

    with SessionLocal() as db:
        lead = _find_lead(db, client_id=cid, agent_id=aid, from_number=num)

        if not lead:
            lead = Lead(
                client_id=cid,
                agent_id=aid,
                instance=inst,
                from_number=num,
                origem=origem,
                status="primeiro_contato",
                first_seen_at=_now_utc(),
                lead_saved=False,
                created_at=_now_utc(),
                updated_at=_now_utc(),
            )
            db.add(lead)
            db.commit()
            return

        # já existe: atualiza last touch
        lead.updated_at = _now_utc()
        db.commit()


def mark_intent(
    *,
    client_id: Optional[str],
    agent_id: Optional[str],
    instance: Optional[str],
    from_number: str,
    intents: list[str],
    origem: str = "intencao",
) -> None:
    """
    Marca intenção (lead quente).
    Atualiza intent_detected, status e origem no registro "corrente" do lead.
    """
    cid = (client_id or DEFAULT_CLIENT_ID or "").strip()
    if not cid:
        raise RuntimeError("client_id vazio (defina CLIENT_ID para legado ou passe client_id dinamicamente).")

    inst = (instance or "").strip()
    aid = (agent_id or "").strip() or None
    num = (from_number or "").strip()
    if not num:
        return

    intent_str = ",".join([i.strip().lower() for i in intents if (i or "").strip()])[:500]

    with SessionLocal() as db:
        lead = _find_lead(db, client_id=cid, agent_id=aid, from_number=num)

        if not lead:
            # se por algum motivo ainda não existe, cria já como lead_quente
            lead = Lead(
                client_id=cid,
                agent_id=aid,
                instance=inst,
                from_number=num,
                origem=origem,
                status="lead_quente",
                intent_detected=intent_str,
                first_seen_at=_now_utc(),
                lead_saved=False,
                created_at=_now_utc(),
                updated_at=_now_utc(),
            )
            db.add(lead)
            db.commit()
            return

        lead.intent_detected = intent_str
        # mantém status mais avançado se já estiver em handoff, senão marca como lead_quente
        if (lead.status or "").strip() not in ("aguardando_atendente", "handoff", "lead_captured"):
            lead.status = "lead_quente"
        lead.origem = origem
        lead.updated_at = _now_utc()
        db.commit()


def save_handoff_lead(
    *,
    client_id: Optional[str],
    agent_id: Optional[str],
    instance: Optional[str],
    from_number: str,
    nome: str,
    telefone: str,
    assunto: str,
    origem: str = "handoff_form",
) -> None:
    """
    Salva/atualiza lead qualificado (handoff).
    Não duplica; atualiza o registro corrente do contato.
    """
    cid = (client_id or DEFAULT_CLIENT_ID or "").strip()
    if not cid:
        raise RuntimeError("client_id vazio (defina CLIENT_ID para legado ou passe client_id dinamicamente).")

    inst = (instance or "").strip()
    aid = (agent_id or "").strip() or None
    num = (from_number or "").strip()

    if not num:
        return

    nome = (nome or "").strip()[:200]
    telefone = (telefone or "").strip()[:80]
    assunto = (assunto or "").strip()[:500]

    with SessionLocal() as db:
        lead = _find_lead(db, client_id=cid, agent_id=aid, from_number=num)

        if not lead:
            lead = Lead(
                client_id=cid,
                agent_id=aid,
                instance=inst,
                from_number=num,
                nome=nome,
                telefone=telefone,
                assunto=assunto,
                status="aguardando_atendente",
                origem=origem,
                lead_saved=True,
                first_seen_at=_now_utc(),
                created_at=_now_utc(),
                updated_at=_now_utc(),
            )
            db.add(lead)
            db.commit()
        else:
            lead.nome = nome or lead.nome
            lead.telefone = telefone or lead.telefone
            lead.assunto = assunto or lead.assunto
            lead.status = "aguardando_atendente"
            lead.origem = origem
            lead.lead_saved = True
            lead.updated_at = _now_utc()
            db.commit()

        # Backup CSV (opcional)
        _append_csv(LEADS_CSV_PATH, {
            "created_at": _safe_str(getattr(lead, "created_at", "")),
            "client_id": cid,
            "agent_id": aid or "",
            "instance": inst,
            "from_number": num,
            "nome": nome,
            "telefone": telefone,
            "assunto": assunto,
        })


def get_last_leads(
    limit: int = 5,
    *,
    client_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> list[dict]:
    """
    Retorna os últimos leads (para painel/admin).
    - Se client_id vier None e existir DEFAULT_CLIENT_ID, usa o default (compat).
    - Se client_id vier None e DEFAULT_CLIENT_ID vazio, retorna global (útil para admin master).
    - Se agent_id vier, filtra por agente.
    """
    lim = max(1, min(int(limit or 5), 500))
    cid = (client_id or DEFAULT_CLIENT_ID or "").strip()
    aid = (agent_id or "").strip()

    with SessionLocal() as db:
        q = select(Lead).order_by(Lead.created_at.desc()).limit(lim)

        if cid:
            q = q.where(Lead.client_id == cid)
        if aid:
            q = q.where(Lead.agent_id == aid)

        rows = db.execute(q).scalars().all()

    out: list[dict] = []
    for r in rows:
        out.append({
            "id": int(r.id) if getattr(r, "id", None) is not None else None,
            "client_id": getattr(r, "client_id", None),
            "agent_id": getattr(r, "agent_id", None),
            "instance": getattr(r, "instance", None),
            "from_number": getattr(r, "from_number", None),
            "nome": getattr(r, "nome", None),
            "telefone": getattr(r, "telefone", None),
            "assunto": getattr(r, "assunto", None),
            "status": getattr(r, "status", None),
            "origem": getattr(r, "origem", None),
            "intent_detected": getattr(r, "intent_detected", None),
            "first_seen_at": _safe_str(getattr(r, "first_seen_at", None)),
            "created_at": _safe_str(getattr(r, "created_at", None)),
            "updated_at": _safe_str(getattr(r, "updated_at", None)),
            "lead_saved": bool(getattr(r, "lead_saved", False)),
        })
    return out
