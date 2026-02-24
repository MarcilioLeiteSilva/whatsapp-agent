"""
app/admin_bootstrap.py

Bootstrap DEV para criar dados mínimos (client + agents) e preencher credenciais
da Evolution por agente (Opção B - SaaS correto).

Este router deve existir APENAS no DEV (ou protegido por token forte).

Regras:
- Cria client_default
- Cria agente001 e agente002 (instances fixas)
- Preenche evolution_base_url e api_key em cada agente
- Idempotente: pode chamar várias vezes sem duplicar

Como funciona a origem de credenciais:
- default_base_url / default_api_key podem vir de:
  1) body JSON
  2) query string
  3) ENV: DEFAULT_EVOLUTION_BASE_URL / DEFAULT_EVOLUTION_API_KEY
- Também aceita overrides por agente (opcional):
  agent_overrides = {
    "agente001": {"base_url": "...", "api_key": "..."},
    "agente002": {"base_url": "...", "api_key": "..."},
  }

Proteção:
- Header: X-ADMIN-TOKEN deve bater com ENV ADMIN_TOKEN (se definido).
"""

from __future__ import annotations

import os
import logging
from typing import Optional, Dict

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import SessionLocal
from .models import Client, Agent

logger = logging.getLogger("agent")
router = APIRouter(prefix="/admin/bootstrap", tags=["admin_bootstrap"])


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def _require_admin_token(req: Request) -> None:
    expected = (os.getenv("ADMIN_TOKEN", "") or "").strip()
    if not expected:
        # Se não tem token configurado, deixa passar (útil em DEV local).
        # Em EasyPanel DEV, recomendo setar ADMIN_TOKEN.
        return

    got = (req.headers.get("X-ADMIN-TOKEN") or "").strip()
    if got != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _env_default_base_url() -> str:
    return _normalize_base_url(os.getenv("DEFAULT_EVOLUTION_BASE_URL", ""))


def _env_default_api_key() -> str:
    return (os.getenv("DEFAULT_EVOLUTION_API_KEY", "") or "").strip()


def _pick_first(*values: Optional[str]) -> str:
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _commit_or_400(db, action: str) -> None:
    """
    Converte IntegrityError (constraints do Postgres) em HTTP 400 ao invés de 500.
    """
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        logger.exception("BOOTSTRAP_DB_INTEGRITY_ERROR action=%s err=%s", action, e)
        raise HTTPException(status_code=400, detail=f"DB constraint error during {action}")


# -----------------------------------------------------------------------------
# Payload
# -----------------------------------------------------------------------------
class AgentOverride(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None


class BootstrapBody(BaseModel):
    client_name: str = "client_default"
    plan: str = "dev"

    # Defaults para todos os agentes (se não houver override)
    default_base_url: Optional[str] = None
    default_api_key: Optional[str] = None

    # Overrides opcionais por instance (ex: "agente001", "agente002")
    agent_overrides: Dict[str, AgentOverride] = Field(default_factory=dict)

    # Lista de instâncias a garantir no bootstrap
    instances: list[str] = Field(default_factory=lambda: ["agente001", "agente002"])

    # Se True, atualiza base_url/api_key mesmo que já exista valor no DB
    force_update_credentials: bool = False


# -----------------------------------------------------------------------------
# Endpoint
# -----------------------------------------------------------------------------
@router.post("")
async def bootstrap(req: Request, body: BootstrapBody):
    """
    Cria/atualiza client_default e agentes básicos no DEV.
    """
    _require_admin_token(req)

    # Também aceita passar defaults via query string
    q_base = req.query_params.get("default_base_url")
    q_key = req.query_params.get("default_api_key")

    default_base_url = _normalize_base_url(
        _pick_first(body.default_base_url, q_base, _env_default_base_url())
    )
    default_api_key = _pick_first(body.default_api_key, q_key, _env_default_api_key())

    if not default_base_url or not (
        default_base_url.startswith("http://") or default_base_url.startswith("https://")
    ):
        # Não quebra o bootstrap inteiro: cria client/agents e retorna aviso.
        logger.warning("BOOTSTRAP_WARN: default_base_url inválida/ausente: %r", default_base_url)

    if not default_api_key:
        logger.warning("BOOTSTRAP_WARN: default_api_key ausente (DEFAULT_EVOLUTION_API_KEY).")

    created = {"client": False, "agents_created": 0, "agents_updated": 0, "warnings": []}

    with SessionLocal() as db:
        # ---------------------------------------------------------------------
        # Client
        # ---------------------------------------------------------------------
        client = db.execute(select(Client).where(Client.name == body.client_name)).scalar_one_or_none()
        if not client:
            client = Client(name=body.client_name, plan=body.plan)
            db.add(client)
            _commit_or_400(db, "create_client")
            db.refresh(client)
            created["client"] = True

        client_id = client.id

        # ---------------------------------------------------------------------
        # Agents
        # ---------------------------------------------------------------------
        for inst in body.instances:
            inst = (inst or "").strip()
            if not inst:
                continue

            agent = db.execute(select(Agent).where(Agent.instance == inst)).scalar_one_or_none()
            is_new = False

            if not agent:
                agent = Agent(
                    client_id=client_id,
                    name=inst,
                    instance=inst,
                    status="active",
                    evolution_base_url=None,
                    api_key=None,
                )
                db.add(agent)
                _commit_or_400(db, f"create_agent:{inst}")
                db.refresh(agent)
                created["agents_created"] += 1
                is_new = True

            # Resolve credenciais: override > default
            ov = body.agent_overrides.get(inst)
            ov_base = _normalize_base_url(ov.base_url) if ov and ov.base_url else ""
            ov_key = (ov.api_key or "").strip() if ov and ov.api_key else ""

            base_to_set = _pick_first(ov_base, default_base_url)
            key_to_set = _pick_first(ov_key, default_api_key)

            # Atualiza credenciais se:
            # - agente novo, ou
            # - force_update_credentials, ou
            # - fields estão vazios
            should_set = (
                is_new
                or body.force_update_credentials
                or not (agent.evolution_base_url or "").strip()
                or not (agent.api_key or "").strip()
            )

            if should_set:
                # Só grava se tiver valores (não sobrescreve com vazio)
                changed = False
                if base_to_set and base_to_set.startswith(("http://", "https://")):
                    if (agent.evolution_base_url or "").strip().rstrip("/") != base_to_set:
                        agent.evolution_base_url = base_to_set
                        changed = True
                elif not base_to_set:
                    created["warnings"].append(f"instance={inst}: base_url vazio (não gravado)")
                else:
                    created["warnings"].append(
                        f"instance={inst}: base_url inválido {base_to_set!r} (não gravado)"
                    )

                if key_to_set:
                    if (agent.api_key or "").strip() != key_to_set:
                        agent.api_key = key_to_set
                        changed = True
                else:
                    created["warnings"].append(f"instance={inst}: api_key vazio (não gravado)")

                if changed:
                    db.add(agent)
                    _commit_or_400(db, f"update_agent_credentials:{inst}")
                    db.refresh(agent)
                    created["agents_updated"] += 1

            logger.info(
                "BOOTSTRAP_AGENT: client_id=%s agent_id=%s instance=%s base_url_set=%s api_key_set=%s",
                client_id,
                agent.id,
                inst,
                bool((agent.evolution_base_url or "").strip()),
                bool((agent.api_key or "").strip()),
            )

    return {
        "ok": True,
        "client_id": client_id,
        **created,
        "defaults_used": {
            "default_base_url_present": bool(default_base_url),
            "default_api_key_present": bool(default_api_key),
        },
        "note": "Se defaults estiverem vazios, configure DEFAULT_EVOLUTION_BASE_URL e DEFAULT_EVOLUTION_API_KEY no DEV.",
    }
