"""
app/admin_web.py

Dashboard web mínimo (DEV) para:
- Clients (lista)
- Agents (lista / criar / editar)
- Leads (lista)
- Simulator (enviar mensagem de teste por instance)

Autenticação simples:
- Se ADMIN_TOKEN estiver definido no env, exige login.
- Login grava cookie "admin_token".
- Em DEV local, se ADMIN_TOKEN vazio, libera.

Config:
- ADMIN_TOKEN: token do painel
- SIMULATOR_BASE_URL: base do whatsapp-simulator (ex: http://whatsapp-simulator:9000)
- ALLOW_SIMULATOR: já existe no seu main.py (não usado aqui diretamente)
"""

from __future__ import annotations

import os
import logging
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from sqlalchemy import select, desc
from .db import SessionLocal
from .models import Client, Agent, Lead

logger = logging.getLogger("agent")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

router = APIRouter(prefix="/admin", tags=["admin_web"])


# -----------------------------------------------------------------------------
# Auth helpers (cookie-based)
# -----------------------------------------------------------------------------
def _expected_admin_token() -> str:
    return (os.getenv("ADMIN_TOKEN", "") or "").strip()


def _is_auth_required() -> bool:
    return bool(_expected_admin_token())


def _is_authed(req: Request) -> bool:
    expected = _expected_admin_token()
    if not expected:
        return True  # DEV aberto se não tem token configurado
    got = (req.cookies.get("admin_token") or "").strip()
    return got == expected


def _require_auth(req: Request) -> Optional[RedirectResponse]:
    if _is_authed(req):
        return None
    # manda pro login
    return RedirectResponse(url="/admin/login", status_code=302)


def _ctx(req: Request, **extra):
    """Contexto comum para templates."""
    return {
        "request": req,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "auth_required": _is_auth_required(),
        "authed": _is_authed(req),
        "simulator_base_url": (os.getenv("SIMULATOR_BASE_URL", "") or "").strip(),
        **extra,
    }


# -----------------------------------------------------------------------------
# Login/logout
# -----------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
async def login_page(req: Request):
    if _is_authed(req):
        return RedirectResponse(url="/admin/dashboard", status_code=302)
    return templates.TemplateResponse("login.html", _ctx(req))


@router.post("/login")
async def login_post(req: Request, token: str = Form(default="")):
    expected = _expected_admin_token()
    if not expected:
        # Se não tem token definido, não faz sentido logar — redireciona direto.
        return RedirectResponse(url="/admin/dashboard", status_code=302)

    if (token or "").strip() != expected:
        return templates.TemplateResponse(
            "login.html",
            _ctx(req, error="Token inválido."),
            status_code=401,
        )

    resp = RedirectResponse(url="/admin/dashboard", status_code=302)
    resp.set_cookie("admin_token", expected, httponly=True, samesite="lax")
    return resp


@router.post("/logout")
async def logout_post():
    resp = RedirectResponse(url="/admin/login", status_code=302)
    resp.delete_cookie("admin_token")
    return resp


# -----------------------------------------------------------------------------
# Dashboard
# -----------------------------------------------------------------------------
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(req: Request):
    redir = _require_auth(req)
    if redir:
        return redir

    with SessionLocal() as db:
        clients_count = db.execute(select(Client)).scalars().all()
        agents_count = db.execute(select(Agent)).scalars().all()

        # últimos leads (20)
        leads = (
            db.execute(select(Lead).order_by(desc(Lead.created_at)).limit(20))
            .scalars()
            .all()
        )

        # métricas básicas
        total_clients = len(clients_count)
        total_agents = len(agents_count)
        total_leads = db.execute(select(Lead)).scalars().all()
        total_leads = len(total_leads)

    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(
            req,
            total_clients=total_clients,
            total_agents=total_agents,
            total_leads=total_leads,
            leads=leads,
        ),
    )


# -----------------------------------------------------------------------------
# Clients
# -----------------------------------------------------------------------------
@router.get("/clients", response_class=HTMLResponse)
async def clients(req: Request):
    redir = _require_auth(req)
    if redir:
        return redir

    with SessionLocal() as db:
        items = db.execute(select(Client).order_by(desc(Client.created_at))).scalars().all()

    return templates.TemplateResponse("clients.html", _ctx(req, clients=items))


@router.post("/clients/create")
async def clients_create(
    req: Request,
    name: str = Form(...),
    plan: str = Form(default="dev"),
):
    redir = _require_auth(req)
    if redir:
        return redir

    name = (name or "").strip()
    plan = (plan or "dev").strip()

    with SessionLocal() as db:
        exists = db.execute(select(Client).where(Client.name == name)).scalar_one_or_none()
        if not exists:
            c = Client(name=name, plan=plan)
            db.add(c)
            db.commit()
            db.refresh(c)
            logger.info("ADMIN_CLIENT_CREATED: client_id=%s name=%s plan=%s", c.id, c.name, c.plan)

    return RedirectResponse(url="/admin/clients", status_code=302)


# -----------------------------------------------------------------------------
# Agents
# -----------------------------------------------------------------------------
@router.get("/agents", response_class=HTMLResponse)
async def agents(req: Request):
    redir = _require_auth(req)
    if redir:
        return redir

    with SessionLocal() as db:
        items = (
            db.execute(select(Agent).order_by(desc(Agent.created_at)))
            .scalars()
            .all()
        )
        clients = db.execute(select(Client).order_by(desc(Client.created_at))).scalars().all()

    return templates.TemplateResponse("agents.html", _ctx(req, agents=items, clients=clients))


@router.post("/agents/create")
async def agents_create(
    req: Request,
    client_id: int = Form(...),
    name: str = Form(...),
    instance: str = Form(...),
    evolution_base_url: str = Form(default=""),
    api_key: str = Form(default=""),
    status: str = Form(default="active"),
):
    redir = _require_auth(req)
    if redir:
        return redir

    name = (name or "").strip()
    instance = (instance or "").strip()
    evolution_base_url = (evolution_base_url or "").strip().rstrip("/")
    api_key = (api_key or "").strip()
    status = (status or "active").strip()

    with SessionLocal() as db:
        # instance deve ser UNIQUE
        exists = db.execute(select(Agent).where(Agent.instance == instance)).scalar_one_or_none()
        if exists:
            logger.warning("ADMIN_AGENT_CREATE_EXISTS: instance=%s agent_id=%s", instance, exists.id)
            return RedirectResponse(url="/admin/agents?err=instance_exists", status_code=302)

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
            "ADMIN_AGENT_CREATED: client_id=%s agent_id=%s instance=%s status=%s",
            a.client_id,
            a.id,
            a.instance,
            a.status,
        )

    return RedirectResponse(url="/admin/agents", status_code=302)


@router.post("/agents/update")
async def agents_update(
    req: Request,
    agent_id: int = Form(...),
    name: str = Form(default=""),
    evolution_base_url: str = Form(default=""),
    api_key: str = Form(default=""),
    status: str = Form(default="active"),
):
    redir = _require_auth(req)
    if redir:
        return redir

    name = (name or "").strip()
    evolution_base_url = (evolution_base_url or "").strip().rstrip("/")
    api_key = (api_key or "").strip()
    status = (status or "active").strip()

    with SessionLocal() as db:
        a = db.execute(select(Agent).where(Agent.id == agent_id)).scalar_one_or_none()
        if not a:
            return RedirectResponse(url="/admin/agents?err=not_found", status_code=302)

        if name:
            a.name = name
        a.evolution_base_url = evolution_base_url or None
        a.api_key = api_key or None
        a.status = status

        db.add(a)
        db.commit()
        db.refresh(a)

        logger.info(
            "ADMIN_AGENT_UPDATED: client_id=%s agent_id=%s instance=%s status=%s",
            a.client_id,
            a.id,
            a.instance,
            a.status,
        )

    return RedirectResponse(url="/admin/agents", status_code=302)


# -----------------------------------------------------------------------------
# Leads
# -----------------------------------------------------------------------------
@router.get("/leads", response_class=HTMLResponse)
async def leads(req: Request):
    redir = _require_auth(req)
    if redir:
        return redir

    with SessionLocal() as db:
        items = (
            db.execute(select(Lead).order_by(desc(Lead.created_at)).limit(200))
            .scalars()
            .all()
        )

    return templates.TemplateResponse("leads.html", _ctx(req, leads=items))


# -----------------------------------------------------------------------------
# Simulator UI (calls whatsapp-simulator)
# -----------------------------------------------------------------------------
@router.get("/simulator", response_class=HTMLResponse)
async def simulator(req: Request):
    redir = _require_auth(req)
    if redir:
        return redir

    with SessionLocal() as db:
        agents = db.execute(select(Agent).order_by(desc(Agent.created_at))).scalars().all()

    return templates.TemplateResponse("simulator.html", _ctx(req, agents=agents))


@router.post("/simulator/send")
async def simulator_send(
    req: Request,
    instance: str = Form(...),
    from_number: str = Form(...),
    text: str = Form(...),
):
    redir = _require_auth(req)
    if redir:
        return redir

    instance = (instance or "").strip()
    from_number = (from_number or "").strip()
    text = (text or "").strip()

    simulator_base = (os.getenv("SIMULATOR_BASE_URL", "") or "").strip().rstrip("/")
    if not simulator_base:
        # Sem simulator configurado: mostra erro na tela
        return templates.TemplateResponse(
            "simulator.html",
            _ctx(req, agents=[], error="SIMULATOR_BASE_URL não configurado no env do whatsapp-agent-dev."),
            status_code=400,
        )

    payload = {"instance": instance, "from_number": from_number, "text": text}

    # Chama simulator
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{simulator_base}/simulate/message", json=payload)
            ok = r.status_code < 400
            body = None
            try:
                body = r.json()
            except Exception:
                body = {"raw": r.text}

        logger.info(
            "ADMIN_SIM_SEND: instance=%s from=%s ok=%s status=%s",
            instance,
            from_number,
            ok,
            r.status_code,
        )

        # volta pra página com resultado
        url = "/admin/simulator?sent=1" if ok else "/admin/simulator?sent=0"
        return RedirectResponse(url=url, status_code=302)

    except Exception as e:
        logger.error("ADMIN_SIM_SEND_ERROR: instance=%s from=%s err=%s", instance, from_number, e)
        return RedirectResponse(url="/admin/simulator?sent=0", status_code=302)
