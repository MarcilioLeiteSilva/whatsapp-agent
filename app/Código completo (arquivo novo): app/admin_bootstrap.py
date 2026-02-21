import os
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from sqlalchemy import select

from .db import SessionLocal
from .models import Client, Agent

router = APIRouter()

# -----------------------------------------------------------------------------
# Segurança: DEV-only + token
# -----------------------------------------------------------------------------
ENV = os.getenv("ENV", "").strip().lower()
ALLOW_SIMULATOR = os.getenv("ALLOW_SIMULATOR", "false").strip().lower() in ("1", "true", "yes", "y")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()


def _dev_only_guard(req: Request) -> None:
    """
    Protege endpoints perigosos (seed/bootstrap).
    Regras:
    - só funciona em DEV (ENV=dev) OU quando simulador está habilitado
    - exige X-ADMIN-TOKEN igual ao ADMIN_TOKEN
    """
    if not (ENV == "dev" or ALLOW_SIMULATOR):
        # 404 para não expor existência do endpoint em produção
        raise HTTPException(status_code=404, detail="not found")

    token = (req.headers.get("X-ADMIN-TOKEN") or "").strip()
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")


@router.post("/admin/bootstrap")
async def admin_bootstrap(
    req: Request,
    client_id: str = "client_default",
    client_name: str = "Cliente Default (DEV)",
    agent1_id: str = "agent_agente001",
    agent1_name: str = "Agente 001 (DEV)",
    agent1_instance: str = "agente001",
    agent2_id: Optional[str] = "agent_agente002",
    agent2_name: str = "Agente 002 (DEV)",
    agent2_instance: str = "agente002",
):
    """
    Bootstrap DEV (seed):
    - cria Client (tenant) se não existir
    - cria 1 ou 2 Agents se não existirem (com instances únicas)

    Para chamar:
    POST /admin/bootstrap
    Header: X-ADMIN-TOKEN: <ADMIN_TOKEN>

    Observação:
    - não cria/migra tabelas. Assume que o DB já tem schema.
      (se você usar Alembic, rode alembic upgrade head antes)
    """
    _dev_only_guard(req)

    created = {"client": False, "agent1": False, "agent2": False}

    with SessionLocal() as db:
        # -------------------------
        # Client
        # -------------------------
        client = db.execute(select(Client).where(Client.id == client_id)).scalar_one_or_none()
        if not client:
            client = Client(id=client_id, name=client_name, plan="basic")
            db.add(client)
            db.commit()
            created["client"] = True

        # -------------------------
        # Agent 1
        # -------------------------
        a1 = db.execute(select(Agent).where(Agent.id == agent1_id)).scalar_one_or_none()
        if not a1:
            inst_used = db.execute(select(Agent).where(Agent.instance == agent1_instance)).scalar_one_or_none()
            if inst_used:
                raise HTTPException(status_code=409, detail=f"instance já existe: {agent1_instance}")

            a1 = Agent(
                id=agent1_id,
                client_id=client_id,
                name=agent1_name,
                instance=agent1_instance,
                status="active",
            )
            db.add(a1)
            db.commit()
            created["agent1"] = True

        # -------------------------
        # Agent 2 (opcional)
        # -------------------------
        if agent2_id and agent2_instance:
            a2 = db.execute(select(Agent).where(Agent.id == agent2_id)).scalar_one_or_none()
            if not a2:
                inst_used = db.execute(select(Agent).where(Agent.instance == agent2_instance)).scalar_one_or_none()
                if inst_used:
                    raise HTTPException(status_code=409, detail=f"instance já existe: {agent2_instance}")

                a2 = Agent(
                    id=agent2_id,
                    client_id=client_id,
                    name=agent2_name,
                    instance=agent2_instance,
                    status="active",
                )
                db.add(a2)
                db.commit()
                created["agent2"] = True

    return {"ok": True, "created": created}
