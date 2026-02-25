# app/admin_clients.py
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from .db import SessionLocal
from .models import Client, Plan

router = APIRouter(prefix="/admin/clients", tags=["admin:clients"])


@router.put("/{client_id}/plan")
def set_client_plan(client_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    payload: { "plan_id": "basic" }
    """
    plan_id = (payload.get("plan_id") or "").strip()
    if not plan_id:
        raise HTTPException(status_code=400, detail="plan_id is required")

    with SessionLocal() as db:
        client = db.execute(select(Client).where(Client.id == client_id)).scalar_one_or_none()
        if not client:
            raise HTTPException(status_code=404, detail="client not found")

        plan = db.execute(select(Plan).where(Plan.id == plan_id)).scalar_one_or_none()
        if not plan:
            raise HTTPException(status_code=404, detail="plan not found")

        client.plan_id = plan_id
        db.commit()

    return {"ok": True, "client_id": client_id, "plan_id": plan_id}
