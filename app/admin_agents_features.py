# app/admin_agents_features.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from .db import SessionLocal
from .models import Agent
from .features import get_effective_features

router = APIRouter(prefix="/admin/agents", tags=["admin:agents:features"])


@router.get("/{agent_id}/effective-features")
def agent_effective_features(agent_id: str) -> Dict[str, Any]:
    with SessionLocal() as db:
        agent = db.execute(select(Agent).where(Agent.id == agent_id)).scalar_one_or_none()
        if not agent:
            raise HTTPException(status_code=404, detail="agent not found")

    eff = get_effective_features(client_id=agent.client_id, agent_id=agent_id)
    return {"ok": True, "agent_id": agent_id, "client_id": agent.client_id, "effective": eff}


@router.put("/{agent_id}/features-override")
def set_agent_override(agent_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    payload:
      { "override": { "handoff_enabled": true, "max_messages_month": 5000 } }
    """
    override = payload.get("override")

    if override is not None and not isinstance(override, dict):
        raise HTTPException(status_code=400, detail="override must be an object or null")

    with SessionLocal() as db:
        agent = db.execute(select(Agent).where(Agent.id == agent_id)).scalar_one_or_none()
        if not agent:
            raise HTTPException(status_code=404, detail="agent not found")

        agent.features_override = override
        agent.features_override_updated_at = datetime.now(timezone.utc)
        db.commit()

    return {"ok": True}
