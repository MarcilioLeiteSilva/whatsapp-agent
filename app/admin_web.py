"""
app/admin_web.py

Painel Admin Web (SSR) para DEV:
- Dashboard: /admin/web
- Clients:   /admin/web/clients
- Agents:    /admin/web/agents
- Leads:     /admin/web/leads
- Simulator: /admin/web/simulator

Compatível com schema atual (leads.client_id TEXT, leads.agent_id TEXT).

Notas:
- Painel roda no whatsapp-agent (DEV).
- Simulator é um serviço separado; aqui fazemos proxy HTTP quando ALLOW_SIMULATOR=true.
- Proteção opcional por ADMIN_TOKEN:
    - Header: X-ADMIN-TOKEN
    - OU query param: ?token=...
    - OU cookie: admin_token
- Templates devem existir em app/templates/
"""

from __future__ import annotations

import os
import time
import logging
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from sqlalchemy import select, func, or_, desc

from .db import SessionLocal
from .models import Client, Agent, Lead  # Ajuste SOMENTE se seu arquivo models.py usar outros nomes

logger = logging.getLogger("agent")

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
router = APIRouter(prefix="/admin/web", tags=["admin_web"])

ALLOW_SIMULATOR = os.getenv("ALLOW_SIMULATOR", "false").strip().lower() in ("1", "true", "yes", "y")
SIMULATOR_BASE_URL = (os.getenv("SIMULATOR_BASE_URL", "http://whatsapp-simulator:8000") or "").strip().rstrip("/")

# Token opcional (recomendado em DEV público)
ADMIN_TOKEN = (os.getenv("ADMIN_TOKEN", "") or "").strip()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _require_admin(req: Request) -> None:
    """
    Se ADMIN_TOKEN estiver configurado, exige token por:
    - X-ADMIN-TOKEN (header)
    - ?token= (query)
    - cookie admin_token
    """
    if not ADMIN_TOKEN:
        return

    got = (req.headers.get("X-ADMIN-TOKEN") or "").strip()
    if not got:
        got = (req.query_params.get("token") or "").strip()
    if not got:
        got = (req.cookies.get("admin_token") or "").strip()

    if got != ADMIN_TOKEN:
        raise PermissionError("unauthorized")


def _flash_from_query(req: Request) -> Optional[dict]:
    kind = (req.query_params.get("flash_kind") or "").strip()
    msg = (req.query_params.get("flash_message") or "").strip()
    if not msg:
        return None
    return {"kind": kind or "info", "message": msg}


def _url(req: Request, name: str, **path_params: Any) -> str:
    return str(req.url_for(name, **path_params))


def _redirect(req: Request, to_name: str, *, flash_kind: str = "info", flash_message: str = "", extra_qs: Optional[dict] = None) -> RedirectResponse:
    url = _url(req, to_name)
    qp = {}
    if flash_message:
        qp["flash_kind"] = flash_kind
        qp["flash_message"] = flash_message
    if extra_qs:
        qp.update(extra_qs)

    if qp:
        q = str(httpx.QueryParams(qp))
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{q}"

    return RedirectResponse(url, status_code=303)


def _normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def _client_to_view(c: Client) -> dict:
    # Não assumimos tipo do id; tratamos como str/Any
    return {
        "id": getattr(c, "id", None),
        "name": getattr(c, "name", None),
        "plan": getattr(c, "plan", None),
        "created_at": str(getattr(c, "created_at", "") or "") or None,
    }


def _agent_to_view(a: Agent) -> dict:
    return {
        "id": getattr(a, "id", None),
        "client_id": getattr(a, "client_id", None),  # pode ser text ou int dependendo do schema
        "name": getattr(a, "name", None),
        "instance": getattr(a, "instance", None),
        "evolution_base_url": getattr(a, "evolution_base_url", None),
        "api_key": getattr(a, "api_key", None),
        "status": getattr(a, "status", None),
        "last_seen_at": str(getattr(a, "last_seen_at", "") or "") or None,
        "created_at": str(getattr(a, "created_at", "") or "") or None,
    }


def _lead_to_view(l: Lead) -> dict:
    # Compatível com seu schema:
    # client_id (text), agent_id (text nullable), instance, from_number, nome, telefone, assunto...
    return {
        "id": getattr(l, "id", None),
        "client_id": getattr(l, "client_id", None),
        "agent_id": getattr(l, "agent_id", None),
        "instance": getattr(l, "instance", None),
        "from_number": getattr(l, "from_number", None),
        "nome": getattr(l, "nome", None),
        "telefone": getattr(l, "telefone", None),
        "assunto": getattr(l, "assunto", None),
        "status": getattr(l, "status", None),
        "origem": getattr(l, "origem", None),
        "intent_detected": getattr(l, "intent_detected", None),
        "created_at": str(getattr(l, "created_at", "") or "") or None,
    }


async def _status_check() -> dict:
    """
    Check simples semelhante ao /status (sem chamar endpoint interno).
    - DB: conta leads
    - Evolution: best effort (usa ENV ou primeiro agent com evolution_base_url)
    """
    # DB check
    db_ok = True
    db_err = None
    try:
        with SessionLocal() as db:
            _ = db.execute(select(func.count()).select_from(Lead)).scalar_one()
    except Exception as e:
        db_ok = False
        db_err = str(e)

    # Evolution check (best effort)
    evo_ok = True
    evo_err = None
    try:
        base = (os.getenv("EVOLUTION_BASE_URL", "") or "").strip().rstrip("/")

        if not base or not base.startswith(("http://", "https://")):
            with SessionLocal() as db:
                a = db.execute(
                    select(Agent)
                    .where(Agent.evolution_base_url.is_not(None))
                    .order_by(desc(Agent.id))
                    .limit(1)
                ).scalar_one_or_none()
            if a and (a.evolution_base_url or "").strip():
                base = (a.evolution_base_url or "").strip().rstrip("/")

        if not base or not base.startswith(("http://", "https://")):
            raise ValueError(f"EVOLUTION_BASE_URL inválida/ausente: {base!r}")

        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(base)
            _ = r.status_code
    except Exception as e:
        evo_ok = False
        evo_err = str(e)

    return {
        "ok": db_ok and evo_ok,
        "db_ok": db_ok,
        "db_err": db_err,
        "evolution_ok": evo_ok,
        "evolution_err": evo_err,
        "allow_simulator": ALLOW_SIMULATOR,
    }


# -----------------------------------------------------------------------------
# Pages
# -----------------------------------------------------------------------------
@router.get("", name="admin_web_dashboard")
async def dashboard(req: Request):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_dashboard", flash_kind="error", flash_message="Unauthorized (token inválido)")

    flash = _flash_from_query(req)

    with SessionLocal() as db:
        # Se suas tabelas clients/agents existirem e estiverem migradas, conta normalmente.
        # Caso não exista, isso geraria erro. Em DEV você já tem essas tabelas.
        clients_count = db.execute(select(func.count()).select_from(Client)).scalar_one()
        agents_count = db.execute(select(func.count()).select_from(Agent)).scalar_one()
        leads_count = db.execute(select(func.count()).select_from(Lead)).scalar_one()
        last_lead_at = db.execute(select(func.max(Lead.created_at))).scalar_one()

        recent = db.execute(select(Lead).order_by(desc(Lead.created_at)).limit(10)).scalars().all()
        recent_leads = [_lead_to_view(l) for l in recent]

    status = await _status_check()

    ctx = {
        "request": req,
        "active_nav": "dashboard",
        "flash": flash,
        "stats": {
            "clients_count": clients_count,
            "agents_count": agents_count,
            "leads_count": leads_count,
            "last_lead_at": str(last_lead_at) if last_lead_at else None,
        },
        "status": status,
        "status_url": "/status",
        "recent_leads": recent_leads,
    }
    return templates.TemplateResponse("dashboard.html", ctx)


@router.get("/clients", name="admin_web_clients")
async def clients_page(req: Request):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_dashboard", flash_kind="error", flash_message="Unauthorized (token inválido)")

    flash = _flash_from_query(req)

    with SessionLocal() as db:
        clients = db.execute(select(Client).order_by(desc(Client.id))).scalars().all()

    ctx = {
        "request": req,
        "active_nav": "clients",
        "flash": flash,
        "clients": [_client_to_view(c) for c in clients],
        "create_client_action": _url(req, "admin_web_clients_create"),
    }
    return templates.TemplateResponse("clients.html", ctx)


@router.post("/clients/create", name="admin_web_clients_create")
async def clients_create(req: Request, name: str = Form(...), plan: str = Form("dev")):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_dashboard", flash_kind="error", flash_message="Unauthorized (token inválido)")

    name = (name or "").strip()
    plan = (plan or "").strip() or "dev"
    if not name:
        return _redirect(req, "admin_web_clients", flash_kind="error", flash_message="Nome do client é obrigatório.")

    with SessionLocal() as db:
        exists = db.execute(select(Client).where(Client.name == name)).scalar_one_or_none()
        if exists:
            return _redirect(req, "admin_web_clients", flash_kind="error", flash_message=f"Client já existe: {name}")

        c = Client(name=name, plan=plan)
        db.add(c)
        db.commit()
        db.refresh(c)

        logger.info("ADMIN_WEB_CLIENT_CREATED: client_id=%s name=%s plan=%s", getattr(c, "id", None), name, plan)

    return _redirect(req, "admin_web_clients", flash_kind="success", flash_message=f"Client criado: {name}")


@router.get("/agents", name="admin_web_agents")
async def agents_page(req: Request):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_dashboard", flash_kind="error", flash_message="Unauthorized (token inválido)")

    flash = _flash_from_query(req)

    with SessionLocal() as db:
        # Clients no painel: se seu schema estiver com client_id text no Agent,
        # a criação pode ser "livre". Ainda assim listamos clients para facilitar.
        clients = db.execute(select(Client).order_by(desc(Client.id))).scalars().all()
        agents = db.execute(select(Agent).order_by(desc(Agent.id))).scalars().all()

    ctx = {
        "request": req,
        "active_nav": "agents",
        "flash": flash,
        "clients": [_client_to_view(c) for c in clients],
        "agents": [_agent_to_view(a) for a in agents],
        "create_agent_action": _url(req, "admin_web_agents_create"),
    }
    return templates.TemplateResponse("agents.html", ctx)


@router.post("/agents/create", name="admin_web_agents_create")
async def agents_create(
    req: Request,
    client_id: str = Form(...),  # <-- TEXT (compat com seu schema de leads e possível schema de agents)
    name: str = Form(""),
    instance: str = Form(...),
    evolution_base_url: str = Form(""),
    api_key: str = Form(""),
    status: str = Form("active"),
):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_dashboard", flash_kind="error", flash_message="Unauthorized (token inválido)")

    client_id = (client_id or "").strip()
    instance = (instance or "").strip()
    if not client_id:
        return _redirect(req, "admin_web_agents", flash_kind="error", flash_message="client_id é obrigatório.")
    if not instance:
        return _redirect(req, "admin_web_agents", flash_kind="error", flash_message="instance é obrigatória.")

    name = (name or "").strip() or instance
    evolution_base_url = _normalize_base_url(evolution_base_url)
    api_key = (api_key or "").strip()
    status = (status or "active").strip()

    with SessionLocal() as db:
        # valida instance única
        exists = db.execute(select(Agent).where(Agent.instance == instance)).scalar_one_or_none()
        if exists:
            return _redirect(req, "admin_web_agents", flash_kind="error", flash_message=f"Instance já existe: {instance}")

        # valida client (se existir tabela clients e for coerente)
        # Aqui aceitamos client_id livre, mas tentamos alertar se não existir.
        c = db.execute(select(Client).where(or_(Client.name == client_id, Client.id == client_id))).scalar_one_or_none()
        if not c:
            logger.warning("ADMIN_WEB_AGENT_CREATE_WARN: client_not_found client_id=%s instance=%s", client_id, instance)

        a = Agent(
            client_id=client_id,
            name=name,
            instance=instance,
            evolution_base_url=evolution_base_url or None,
            api_key=api_key or None,
            status=status,
        )
        db.add(a)
        db.commit()
        db.refresh(a)

        logger.info(
            "ADMIN_WEB_AGENT_CREATED: client_id=%s agent_id=%s instance=%s base_url_set=%s api_key_set=%s",
            getattr(a, "client_id", None),
            getattr(a, "id", None),
            getattr(a, "instance", None),
            bool((getattr(a, "evolution_base_url", "") or "").strip()),
            bool((getattr(a, "api_key", "") or "").strip()),
        )

    return _redirect(req, "admin_web_agents", flash_kind="success", flash_message=f"Agent criado: {instance}")


@router.get("/leads", name="admin_web_leads")
async def leads_page(req: Request, q: str = ""):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_dashboard", flash_kind="error", flash_message="Unauthorized (token inválido)")

    flash = _flash_from_query(req)
    q = (q or "").strip()

    with SessionLocal() as db:
        stmt = select(Lead).order_by(desc(Lead.created_at)).limit(200)

        if q:
            like = f"%{q}%"
            stmt = (
                select(Lead)
                .where(
                    or_(
                        Lead.from_number.ilike(like),
                        Lead.nome.ilike(like),
                        Lead.telefone.ilike(like),
                        Lead.assunto.ilike(like),
                        Lead.instance.ilike(like),
                        Lead.client_id.ilike(like),
                        Lead.agent_id.ilike(like),
                    )
                )
                .order_by(desc(Lead.created_at))
                .limit(200)
            )

        leads = db.execute(stmt).scalars().all()

    ctx = {
        "request": req,
        "active_nav": "leads",
        "flash": flash,
        "q": q,
        "leads": [_lead_to_view(l) for l in leads],
    }
    return templates.TemplateResponse("leads.html", ctx)


@router.get("/simulator", name="admin_web_simulator")
async def simulator_page(req: Request):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_dashboard", flash_kind="error", flash_message="Unauthorized (token inválido)")

    flash = _flash_from_query(req)

    with SessionLocal() as db:
        agents = db.execute(select(Agent).order_by(desc(Agent.id))).scalars().all()
        instances = [getattr(a, "instance", None) for a in agents if getattr(a, "instance", None)]

    ctx = {
        "request": req,
        "active_nav": "simulator",
        "flash": flash,
        "instances": instances,
        "simulate_action": _url(req, "admin_web_simulator_send"),
        "default_from_number": "5531999999999",
        "default_text": "",
        "last_simulation": req.query_params.get("last_simulation") or "",
    }
    return templates.TemplateResponse("simulator.html", ctx)


@router.post("/simulator/send", name="admin_web_simulator_send")
async def simulator_send(
    req: Request,
    instance: str = Form(...),
    from_number: str = Form("5531999999999"),
    text: str = Form("Oi"),
):
    """
    Faz proxy para o whatsapp-simulator, que envia um evento fake para o webhook DEV.
    """
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_dashboard", flash_kind="error", flash_message="Unauthorized (token inválido)")

    if not ALLOW_SIMULATOR:
        return _redirect(req, "admin_web_simulator", flash_kind="error", flash_message="Simulator desabilitado (ALLOW_SIMULATOR=false).")

    instance = (instance or "").strip()
    from_number = (from_number or "").strip()
    text = (text or "").strip()

    if not instance or not from_number or not text:
        return _redirect(req, "admin_web_simulator", flash_kind="error", flash_message="instance, from_number e text são obrigatórios.")

    payload = {"instance": instance, "from_number": from_number, "text": text}

    t0 = time.time()
    try:
        url = f"{SIMULATOR_BASE_URL}/simulate/message"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.error("SIMULATOR_PROXY_ERROR: instance=%s err=%s simulator_base=%s", instance, e, SIMULATOR_BASE_URL)
        return _redirect(req, "admin_web_simulator", flash_kind="error", flash_message=f"Falha ao chamar simulator: {e}")

    elapsed = round((time.time() - t0) * 1000)

    logger.info(
        "SIMULATED_MESSAGE_SENT: instance=%s from=%s ms=%s simulator_base=%s resp_type=%s",
        instance,
        from_number,
        elapsed,
        SIMULATOR_BASE_URL,
        type(data).__name__,
    )

    last = f"OK instance={instance} from={from_number} ms={elapsed} resp_keys={list(data.keys()) if isinstance(data, dict) else 'n/a'}"

    return _redirect(
        req,
        "admin_web_simulator",
        flash_kind="success",
        flash_message="Simulação enviada com sucesso.",
        extra_qs={"last_simulation": last},
    )
