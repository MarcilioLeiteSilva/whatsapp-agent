import os
import csv
from datetime import datetime, timezone
from sqlalchemy import select, update
from .db import SessionLocal
from .models import Lead

CLIENT_ID = os.getenv("CLIENT_ID", "default")
CSV_PATH = os.getenv("LEADS_CSV_PATH", "/opt/whatsapp-agent/leads.csv")
CSV_BACKUP_ENABLED = os.getenv("CSV_BACKUP_ENABLED", "true").lower() in ("1", "true", "yes", "y")

def _utc_now():
    return datetime.now(timezone.utc)

def ensure_first_contact(client_id, agent_id, instance, from_number): -> int:
    """
    Cria um registro 'iniciado' no primeiro contato.
    Retorna o id do lead mais recente para esse número/instância/cliente.
    """
    with SessionLocal() as db:
        row = db.execute(
            select(Lead).where(
                Lead.instance == instance,
                Lead.from_number == from_number,
                Lead.client_id == CLIENT_ID,
            ).order_by(Lead.created_at.desc()).limit(1)
        ).scalar_one_or_none()

        if row:
            return row.id

        new_lead = Lead(
            client_id=client_id,
            agent_id=agent_id,
            instance=instance,
            from_number=from_number,
            status="iniciado",
            status="primeiro_contato"
        )
        
        db.add(new_lead)
        db.commit()
        db.refresh(new_lead)
        return new_lead.id

def mark_intent(instance: str, from_number: str, intents: list[str]) -> None:
    if not intents:
        return

    lead_id = ensure_first_contact(instance, from_number)
    intent_str = ",".join(sorted(set(intents)))

    with SessionLocal() as db:
        db.execute(
            update(Lead)
            .where(Lead.id == lead_id)
            .values(
                status="lead_quente",
                origem="intencao",
                intent_detected=intent_str,
                updated_at=_utc_now(),
            )
        )
        db.commit()

def save_handoff_lead(instance: str, from_number: str, nome: str, telefone: str, assunto: str) -> int:
    """
    Salva lead do handoff (apenas uma vez por conversa lógica).
    """
    lead_id = ensure_first_contact(instance, from_number)

    with SessionLocal() as db:
        lead = db.execute(select(Lead).where(Lead.id == lead_id)).scalar_one()
        if lead.lead_saved:
            return lead_id

        db.execute(
            update(Lead)
            .where(Lead.id == lead_id)
            .values(
                nome=nome,
                telefone=telefone,
                assunto=assunto,
                status="aguardando_atendente",
                origem="handoff_form",
                lead_saved=True,
                updated_at=_utc_now(),
            )
        )
        db.commit()

    if CSV_BACKUP_ENABLED:
        _append_csv(instance, from_number, nome, telefone, assunto)

    return lead_id

def _append_csv(instance: str, from_number: str, nome: str, telefone: str, assunto: str) -> None:
    dt = _utc_now().isoformat()
    row = [dt, instance, from_number, nome, telefone, assunto, dt]

    file_exists = os.path.exists(CSV_PATH)
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)

    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow(["date_time","instance","from_number","nome","telefone","assunto","lead_timestamp"])
        w.writerow(row)

def get_last_leads(limit: int = 5) -> list[dict]:
    with SessionLocal() as db:
        rows = db.execute(
            select(Lead)
            .where(Lead.client_id == CLIENT_ID)
            .order_by(Lead.created_at.desc())
            .limit(limit)
        ).scalars().all()

    out = []
    for r in rows:
        
      out.append({
            "id": int(r.id) if r.id is not None else None,
            "created_at": str(r.created_at),
            "updated_at": str(r.updated_at) if getattr(r, "updated_at", None) else None,
            "first_seen_at": str(r.first_seen_at) if getattr(r, "first_seen_at", None) else None,
            "instance": r.instance,
            "from_number": r.from_number,
            "nome": r.nome,
            "telefone": r.telefone,
            "assunto": r.assunto,
            "status": r.status,
            "origem": r.origem,
            "intent_detected": r.intent_detected,
        })  
    
    return out
