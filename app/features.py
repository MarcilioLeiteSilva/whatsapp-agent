# app/features.py
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import select
from .db import SessionLocal
from .models import Client, Agent, PlanFeature


def get_client_plan_id(client: Client) -> str:
    """
    Compat:
    - se client.plan_id existir, usa ele
    - senão usa client.plan (legado)
    - senão cai em 'basic'
    """
    pid = (getattr(client, "plan_id", None) or "").strip()
    if pid:
        return pid

    legacy = (getattr(client, "plan", None) or "").strip()
    if legacy:
        return legacy

    return "basic"


def get_effective_features(*, client_id: str, agent_id: str) -> Dict[str, Any]:
    with SessionLocal() as db:
        client = db.execute(select(Client).where(Client.id == client_id)).scalar_one()
        agent = db.execute(select(Agent).where(Agent.id == agent_id)).scalar_one()

        plan_id = get_client_plan_id(client)

        rows = db.execute(
            select(PlanFeature).where(PlanFeature.plan_id == plan_id)
        ).scalars().all()

        base: Dict[str, Any] = {r.key: r.value_json for r in rows}
        override: Dict[str, Any] = agent.features_override or {}

        # Se o plano não permite override, ignora override do agente
        if not bool(base.get("allow_agent_overrides", False)):
            override = {}

        base.update(override)
        base["_plan_id"] = plan_id
        return base
