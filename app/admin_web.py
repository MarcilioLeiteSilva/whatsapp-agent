"""
app/admin_web.py

Painel Admin Web (SSR) para DEV:
- Login:     /admin/web/login
- Logout:    /admin/web/logout
- Dashboard: /admin/web
- Clients:   /admin/web/clients
- Agents:    /admin/web/agents
- Leads:     /admin/web/leads
- Leads CSV: /admin/web/leads/export.csv
- Monitor:   /admin/web/monitor
- Simulator: /admin/web/simulator
- ChatLab:   /admin/web/chatlab
- Agent Rules Editor:
    - GET  /admin/web/agents/{agent_id}/rules
    - POST /admin/web/agents/{agent_id}/rules/save
    - POST /admin/web/agents/{agent_id}/rules/reset

Notas:
- Token do portal: POST /admin/web/clients/{client_id}/token (name="admin_web_clients_generate_token")
- NÃO gere url_for dessa rota sem client_id no clients_page, senão dá NoMatchFound.
"""

from __future__ import annotations

import os
import time
import secrets
import logging
import json
import csv
import io
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse, Response
from starlette.templating import Jinja2Templates

from sqlalchemy import select, func, or_, desc

from .db import SessionLocal
from .models import Client, Agent, Lead, AgentCheck

from .store import MemoryStore
from .rules import reply_for, detect_intents
from .lead_logger import ensure_first_contact, mark_intent, save_handoff_lead, get_agent_by_instance
from .rules_engine import invalidate_agent_rules

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

logger = logging.getLogger("agent")

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
router = APIRouter(prefix="/admin/web", tags=["admin_web"])

# ENV
ALLOW_SIMULATOR = os.getenv("ALLOW_SIMULATOR", "false").strip().lower() in ("1", "true", "yes", "y")
SIMULATOR_BASE_URL = (os.getenv("SIMULATOR_BASE_URL", "http://whatsapp-simulator:8000") or "").strip().rstrip("/")

ADMIN_USER = (os.getenv("ADMIN_USER", "admin") or "").strip()
ADMIN_TOKEN = (os.getenv("ADMIN_TOKEN", "") or "").strip()

BR_TZ = os.getenv("APP_TIMEZONE", "America/Sao_Paulo").strip() or "America/Sao_Paulo"
ONLINE_SECONDS = int(os.getenv("AGENT_ONLINE_SECONDS", "120"))  # heurística: last_seen_at <= 120s => online

chatlab_store = MemoryStore()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _tz():
    if ZoneInfo:
        try:
            return ZoneInfo(BR_TZ)
        except Exception:
            return None
    return None


def _fmt_dt_br(dt) -> str:
    if not dt:
        return "-"
    try:
        tz = _tz()
        if tz:
            dt = dt.astimezone(tz)
        return dt.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return str(dt)


def _is_online(last_seen_at) -> bool:
    if not last_seen_at:
        return False
    try:
        now = time.time()
        age = now - last_seen_at.timestamp()
        return age <= ONLINE_SECONDS
    except Exception:
        return False


def _url(req: Request, name: str, **path_params: Any) -> str:
    return str(req.url_for(name, **path_params))


def _flash_from_query(req: Request) -> Optional[dict]:
    kind = (req.query_params.get("flash_kind") or "").strip()
    msg = (req.query_params.get("flash_message") or "").strip()
    if not msg:
        return None
    return {"kind": kind or "info", "message": msg}


def _redirect(
    req: Request,
    to_name: str,
    *,
    flash_kind: str = "info",
    flash_message: str = "",
    extra_qs: Optional[dict] = None,
    path_params: Optional[dict] = None,
) -> RedirectResponse:
    url = _url(req, to_name, **(path_params or {}))
    qp: dict[str, str] = {}

    if flash_message:
        qp["flash_kind"] = flash_kind
        qp["flash_message"] = flash_message

    if extra_qs:
        for k, v in extra_qs.items():
            if v is None:
                continue
            qp[str(k)] = str(v)

    if qp:
        q = str(httpx.QueryParams(qp))
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{q}"

    return RedirectResponse(url, status_code=303)


def _new_id(prefix: str) -> str:
    # id TEXT sem default no Postgres -> precisamos gerar
    return f"{prefix}_{secrets.token_hex(6)}"


def _normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def _require_admin(req: Request) -> None:
    """
    Auth cookie-first.
    Aceita também header/query para debug.
    """
    if not ADMIN_TOKEN:
        # DEV: se não setou ADMIN_TOKEN, não bloqueia
        return

    got = (req.cookies.get("admin_token") or "").strip()
    if not got:
        got = (req.headers.get("X-ADMIN-TOKEN") or "").strip()
    if not got:
        got = (req.query_params.get("token") or "").strip()

    if got != ADMIN_TOKEN:
        raise PermissionError("unauthorized")


def _client_to_view(c: Client) -> dict:
    return {
        "id": getattr(c, "id", None),
        "name": getattr(c, "name", None),
        "plan": getattr(c, "plan", None),
        "created_at": _fmt_dt_br(getattr(c, "created_at", None)),
        "login_token": getattr(c, "login_token", None),
        "login_token_created_at": _fmt_dt_br(getattr(c, "login_token_created_at", None))
        if getattr(c, "login_token_created_at", None)
        else None,
        "login_token_last_used_at": _fmt_dt_br(getattr(c, "login_token_last_used_at", None))
        if getattr(c, "login_token_last_used_at", None)
        else None,
    }


def _agent_to_view(a: Agent, client_name: Optional[str] = None) -> dict:
    last_seen = getattr(a, "last_seen_at", None)
    return {
        "id": getattr(a, "id", None),
        "client_id": getattr(a, "client_id", None),
        "client_name": client_name,
        "name": getattr(a, "name", None),
        "instance": getattr(a, "instance", None),
        "evolution_base_url": getattr(a, "evolution_base_url", None),
        "api_key": getattr(a, "api_key", None),
        "status": getattr(a, "status", None),
        "last_seen_at": _fmt_dt_br(last_seen) if last_seen else None,
        "online": _is_online(last_seen),
        "created_at": _fmt_dt_br(getattr(a, "created_at", None)),
    }


def _lead_to_view(l: Lead, client_name: Optional[str] = None, agent_name: Optional[str] = None) -> dict:
    return {
        "id": getattr(l, "id", None),
        "client_id": getattr(l, "client_id", None),
        "client_name": client_name,
        "agent_id": getattr(l, "agent_id", None),
        "agent_name": agent_name,
        "instance": getattr(l, "instance", None),
        "from_number": getattr(l, "from_number", None),
        "nome": getattr(l, "nome", None),
        "telefone": getattr(l, "telefone", None),
        "assunto": getattr(l, "assunto", None),
        "status": getattr(l, "status", None),
        "origem": getattr(l, "origem", None),
        "intent_detected": getattr(l, "intent_detected", None),
        "created_at": _fmt_dt_br(getattr(l, "created_at", None)),
    }


async def _status_check() -> dict:
    # DB check
    db_ok = True
    db_err = None
    try:
        with SessionLocal() as db:
            _ = db.execute(select(func.count()).select_from(Lead)).scalar_one()
    except Exception as e:
        db_ok = False
        db_err = str(e)

    # Evolution reachability (best effort)
    evo_ok = True
    evo_err = None
    try:
        base = (os.getenv("EVOLUTION_BASE_URL", "") or "").strip().rstrip("/")
        if not base or not base.startswith(("http://", "https://")):
            with SessionLocal() as db:
                a = (
                    db.execute(
                        select(Agent)
                        .where(Agent.evolution_base_url.is_not(None))
                        .order_by(desc(Agent.created_at))
                        .limit(1)
                    )
                    .scalar_one_or_none()
                )
            if a and (a.evolution_base_url or "").strip():
                base = (a.evolution_base_url or "").strip().rstrip("/")

        if not base or not base.startswith(("http://", "https://")):
            raise ValueError(f"Evolution base_url inválida/ausente: {base!r}")

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
# Auth pages
# -----------------------------------------------------------------------------
@router.get("/login", name="admin_web_login")
async def login_page(req: Request):
    flash = _flash_from_query(req)
    ctx = {
        "request": req,
        "active_nav": "",
        "flash": flash,
        "login_action": _url(req, "admin_web_login_post"),
    }
    return templates.TemplateResponse("login.html", ctx)


@router.post("/login", name="admin_web_login_post")
async def login_post(req: Request, username: str = Form(""), token: str = Form("")):
    username = (username or "").strip()
    token = (token or "").strip()

    if ADMIN_TOKEN:
        if username != ADMIN_USER or token != ADMIN_TOKEN:
            return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Usuário ou token inválidos.")

    resp = _redirect(req, "admin_web_dashboard", flash_kind="success", flash_message="Login realizado.")

    resp.set_cookie(
        key="admin_token",
        value=ADMIN_TOKEN or token or "dev",
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=60 * 60 * 12,  # 12h
    )
    return resp


@router.get("/logout", name="admin_web_logout")
async def logout(req: Request):
    resp = _redirect(req, "admin_web_login", flash_kind="success", flash_message="Você saiu do painel.")
    resp.delete_cookie("admin_token")
    return resp


# -----------------------------------------------------------------------------
# Pages (protected)
# -----------------------------------------------------------------------------
@router.get("", name="admin_web_dashboard")
async def dashboard(req: Request):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Faça login para acessar.")

    flash = _flash_from_query(req)

    with SessionLocal() as db:
        clients_count = db.execute(select(func.count()).select_from(Client)).scalar_one()
        agents_count = db.execute(select(func.count()).select_from(Agent)).scalar_one()
        leads_count = db.execute(select(func.count()).select_from(Lead)).scalar_one()
        last_lead_at = db.execute(select(func.max(Lead.created_at))).scalar_one()

        recent = db.execute(select(Lead).order_by(desc(Lead.created_at)).limit(10)).scalars().all()

        client_ids = {str(getattr(l, "client_id", "")) for l in recent if getattr(l, "client_id", None)}
        agent_ids = {str(getattr(l, "agent_id", "")) for l in recent if getattr(l, "agent_id", None)}

        clients_map: dict[str, str] = {}
        if client_ids:
            for c in db.execute(select(Client).where(Client.id.in_(client_ids))).scalars().all():
                clients_map[str(c.id)] = str(getattr(c, "name", "") or c.id)

        agents_map: dict[str, str] = {}
        if agent_ids:
            for a in db.execute(select(Agent).where(Agent.id.in_(agent_ids))).scalars().all():
                agents_map[str(a.id)] = str(getattr(a, "name", "") or a.id)

        recent_leads = [
            _lead_to_view(
                l,
                client_name=clients_map.get(str(getattr(l, "client_id", ""))),
                agent_name=agents_map.get(str(getattr(l, "agent_id", ""))) if getattr(l, "agent_id", None) else None,
            )
            for l in recent
        ]

    status = await _status_check()

    ctx = {
        "request": req,
        "active_nav": "dashboard",
        "flash": flash,
        "stats": {
            "clients_count": clients_count,
            "agents_count": agents_count,
            "leads_count": leads_count,
            "last_lead_at": _fmt_dt_br(last_lead_at) if last_lead_at else None,
        },
        "status": status,
        "status_url": "/status",
        "recent_leads": recent_leads,
    }
    return templates.TemplateResponse("dashboard.html", ctx)


# -----------------------------------------------------------------------------
# Clients
# -----------------------------------------------------------------------------
@router.get("/clients", name="admin_web_clients")
async def clients_page(req: Request):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Faça login para acessar.")

    flash = _flash_from_query(req)

    with SessionLocal() as db:
        clients = db.execute(select(Client).order_by(desc(Client.created_at))).scalars().all()

    # ✅ NÃO tentar montar URL de rota com path param aqui
    ctx = {
        "request": req,
        "active_nav": "clients",
        "flash": flash,
        "clients": [_client_to_view(c) for c in clients],
        "create_client_action": _url(req, "admin_web_clients_create"),
    }
    return templates.TemplateResponse("clients.html", ctx)


@router.post("/clients/create", name="admin_web_clients_create")
async def clients_create(req: Request, name: str = Form(...), plan: str = Form("basic"), client_id: str = Form("")):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Faça login para acessar.")

    name = (name or "").strip()
    plan = (plan or "").strip() or "basic"
    client_id = (client_id or "").strip()

    if not name:
        return _redirect(req, "admin_web_clients", flash_kind="error", flash_message="Nome do client é obrigatório.")

    if not client_id:
        client_id = _new_id("c")

    with SessionLocal() as db:
        exists_id = db.execute(select(Client).where(Client.id == client_id)).scalar_one_or_none()
        if exists_id:
            return _redirect(req, "admin_web_clients", flash_kind="error", flash_message=f"Client id já existe: {client_id}")

        exists_name = db.execute(select(Client).where(Client.name == name)).scalar_one_or_none()
        if exists_name:
            return _redirect(req, "admin_web_clients", flash_kind="error", flash_message=f"Client name já existe: {name}")

        c = Client(id=client_id, name=name, plan=plan)
        db.add(c)
        db.commit()
        db.refresh(c)

        logger.info("ADMIN_WEB_CLIENT_CREATED: client_id=%s name=%s plan=%s", c.id, name, plan)

    return _redirect(req, "admin_web_clients", flash_kind="success", flash_message=f"Client criado: {name} (id={client_id})")


def _gen_token6() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


@router.post("/clients/{client_id}/token", name="admin_web_clients_generate_token")
async def clients_generate_token(req: Request, client_id: str):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Faça login para acessar.")

    client_id = (client_id or "").strip()
    if not client_id:
        return _redirect(req, "admin_web_clients", flash_kind="error", flash_message="client_id inválido.")

    token = _gen_token6()

    with SessionLocal() as db:
        c = db.execute(select(Client).where(Client.id == client_id).limit(1)).scalar_one_or_none()
        if not c:
            return _redirect(req, "admin_web_clients", flash_kind="error", flash_message=f"Client não encontrado: {client_id}")

        c.login_token = token
        c.login_token_created_at = func.now()
        db.add(c)
        db.commit()

    return _redirect(req, "admin_web_clients", flash_kind="success", flash_message=f"Token gerado para {client_id}: {token}")


# -----------------------------------------------------------------------------
# Agents
# -----------------------------------------------------------------------------
@router.get("/agents", name="admin_web_agents")
async def agents_page(req: Request):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Faça login para acessar.")

    flash = _flash_from_query(req)

    with SessionLocal() as db:
        clients = db.execute(select(Client).order_by(desc(Client.created_at))).scalars().all()
        clients_map = {str(c.id): str(getattr(c, "name", "") or c.id) for c in clients}

        agents = db.execute(select(Agent).order_by(desc(Agent.created_at))).scalars().all()
        agents_view = [_agent_to_view(a, client_name=clients_map.get(str(a.client_id))) for a in agents]

    ctx = {
        "request": req,
        "active_nav": "agents",
        "flash": flash,
        "clients": [_client_to_view(c) for c in clients],
        "agents": agents_view,
        "create_agent_action": _url(req, "admin_web_agents_create"),
    }
    return templates.TemplateResponse("agents.html", ctx)


@router.post("/agents/create", name="admin_web_agents_create")
async def agents_create(
    req: Request,
    client_id: str = Form(...),
    name: str = Form(""),
    instance: str = Form(...),
    evolution_base_url: str = Form(""),
    api_key: str = Form(""),
    status: str = Form("active"),
    agent_id: str = Form(""),
):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Faça login para acessar.")

    client_id = (client_id or "").strip()
    instance = (instance or "").strip()
    if not client_id:
        return _redirect(req, "admin_web_agents", flash_kind="error", flash_message="client_id é obrigatório.")
    if not instance:
        return _redirect(req, "admin_web_agents", flash_kind="error", flash_message="instance é obrigatória.")

    name = (name or "").strip() or instance
    evolution_base_url = _normalize_base_url(evolution_base_url)
    api_key = (api_key or "").strip()
    status = (status or "active").strip() or "active"
    agent_id = (agent_id or "").strip() or _new_id("a")

    with SessionLocal() as db:
        c = db.execute(select(Client).where(Client.id == client_id)).scalar_one_or_none()
        if not c:
            return _redirect(req, "admin_web_agents", flash_kind="error", flash_message=f"Client inválido: {client_id}")

        exists_id = db.execute(select(Agent).where(Agent.id == agent_id)).scalar_one_or_none()
        if exists_id:
            return _redirect(req, "admin_web_agents", flash_kind="error", flash_message=f"Agent id já existe: {agent_id}")

        exists_instance = db.execute(select(Agent).where(Agent.instance == instance)).scalar_one_or_none()
        if exists_instance:
            return _redirect(req, "admin_web_agents", flash_kind="error", flash_message=f"Instance já existe: {instance}")

        a = Agent(
            id=agent_id,
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
            a.client_id,
            a.id,
            a.instance,
            bool((a.evolution_base_url or "").strip()),
            bool((a.api_key or "").strip()),
        )

    return _redirect(req, "admin_web_agents", flash_kind="success", flash_message=f"Agent criado: {instance} (id={agent_id})")


# -----------------------------------------------------------------------------
# Leads + CSV
# -----------------------------------------------------------------------------
@router.get("/leads", name="admin_web_leads")
async def leads_page(req: Request, q: str = ""):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Faça login para acessar.")

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

        client_ids = {str(getattr(l, "client_id", "")) for l in leads if getattr(l, "client_id", None)}
        agent_ids = {str(getattr(l, "agent_id", "")) for l in leads if getattr(l, "agent_id", None)}

        clients_map: dict[str, str] = {}
        if client_ids:
            for c in db.execute(select(Client).where(Client.id.in_(client_ids))).scalars().all():
                clients_map[str(c.id)] = str(getattr(c, "name", "") or c.id)

        agents_map: dict[str, str] = {}
        if agent_ids:
            for a in db.execute(select(Agent).where(Agent.id.in_(agent_ids))).scalars().all():
                agents_map[str(a.id)] = str(getattr(a, "name", "") or a.id)

        leads_view = [
            _lead_to_view(
                l,
                client_name=clients_map.get(str(getattr(l, "client_id", ""))),
                agent_name=agents_map.get(str(getattr(l, "agent_id", ""))) if getattr(l, "agent_id", None) else None,
            )
            for l in leads
        ]

    ctx = {
        "request": req,
        "active_nav": "leads",
        "flash": flash,
        "q": q,
        "leads": leads_view,
        "export_csv_url": _url(req, "admin_web_leads_export_csv"),
    }
    return templates.TemplateResponse("leads.html", ctx)


@router.get("/leads/export.csv", name="admin_web_leads_export_csv")
async def leads_export_csv(req: Request, q: str = ""):
    """
    Export simples de leads para CSV (últimos 200, com filtro q).
    """
    try:
        _require_admin(req)
    except PermissionError:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

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

        rows = db.execute(stmt).scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "created_at",
            "client_id",
            "agent_id",
            "instance",
            "from_number",
            "nome",
            "telefone",
            "assunto",
            "intent_detected",
            "status",
            "origem",
        ]
    )

    for l in rows:
        writer.writerow(
            [
                _fmt_dt_br(getattr(l, "created_at", None)),
                getattr(l, "client_id", "") or "",
                getattr(l, "agent_id", "") or "",
                getattr(l, "instance", "") or "",
                getattr(l, "from_number", "") or "",
                getattr(l, "nome", "") or "",
                getattr(l, "telefone", "") or "",
                getattr(l, "assunto", "") or "",
                getattr(l, "intent_detected", "") or "",
                getattr(l, "status", "") or "",
                getattr(l, "origem", "") or "",
            ]
        )

    filename = "leads_export.csv"
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -----------------------------------------------------------------------------
# Simulator
# -----------------------------------------------------------------------------
@router.get("/simulator", name="admin_web_simulator")
async def simulator_page(req: Request):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Faça login para acessar.")

    flash = _flash_from_query(req)

    with SessionLocal() as db:
        agents = db.execute(select(Agent).order_by(desc(Agent.created_at))).scalars().all()
        instances = [a.instance for a in agents if getattr(a, "instance", None)]

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
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Faça login para acessar.")

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
    last = f"OK instance={instance} from={from_number} ms={elapsed} resp_keys={list(data.keys()) if isinstance(data, dict) else 'n/a'}"

    return _redirect(
        req,
        "admin_web_simulator",
        flash_kind="success",
        flash_message="Simulação enviada com sucesso.",
        extra_qs={"last_simulation": last},
    )


# -----------------------------------------------------------------------------
# ChatLab
# -----------------------------------------------------------------------------
@router.get("/chatlab", name="admin_web_chatlab")
async def chatlab_page(req: Request):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Faça login para acessar.")

    flash = _flash_from_query(req)

    with SessionLocal() as db:
        agents = db.execute(select(Agent).order_by(desc(Agent.created_at))).scalars().all()
        instances = [a.instance for a in agents if getattr(a, "instance", None)]

    ctx = {
        "request": req,
        "active_nav": "chatlab",
        "flash": flash,
        "instances": instances,
        "send_url": _url(req, "admin_web_chatlab_send"),
    }
    return templates.TemplateResponse("chatlab.html", ctx)


@router.post("/chatlab/send", name="admin_web_chatlab_send")
async def chatlab_send(req: Request):
    """
    Simula mensagem INBOUND para um agent instance e devolve o reply do bot
    SEM enviar pra Evolution (ideal para testar rules.py).

    Payload JSON:
    {
      "instance": "agente001",
      "from_number": "5531999999999",
      "text": "Oi"
    }
    """
    try:
        _require_admin(req)
    except PermissionError:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_json"}, status_code=400)

    instance = (body.get("instance") or "").strip()
    from_number = (body.get("from_number") or "").strip()
    text = (body.get("text") or "").strip()

    if not instance or not from_number or not text:
        return JSONResponse({"ok": False, "error": "missing_fields"}, status_code=400)

    agent = get_agent_by_instance(instance)
    if not agent:
        return JSONResponse({"ok": False, "error": "unknown_instance"}, status_code=404)

    client_id = agent.client_id
    agent_id = agent.id

    # Captura automática (igual webhook)
    try:
        ensure_first_contact(client_id=client_id, agent_id=agent_id, instance=instance, from_number=from_number)

        intents = detect_intents(text)
        if intents:
            mark_intent(client_id=client_id, agent_id=agent_id, instance=instance, from_number=from_number, intents=intents)
    except Exception as e:
        logger.error("CHATLAB_LEAD_CAPTURE_ERROR: client_id=%s agent_id=%s instance=%s err=%s", client_id, agent_id, instance, e)

    # Estado (memória curta) – store local do ChatLab (isolado por agent)
    state_key = f"{agent_id}:{from_number}"
    state = chatlab_store.get_state(state_key)

    ctx = {"client_id": client_id, "agent_id": agent_id, "instance": instance}
    reply = reply_for(from_number, text, state, ctx=ctx)

    # Se pausado (handoff)
    if reply is None:
        return JSONResponse({"ok": True, "paused": True, "reply": None, "state": state})

    # Persistência lead (uma vez só) – igual ao webhook
    try:
        if state and state.get("step") == "lead_captured" and state.get("lead") and not state.get("lead_saved"):
            lead = state.get("lead") or {}
            nome = (lead.get("nome") or "").strip()
            telefone = (lead.get("telefone") or "").strip()
            assunto = (lead.get("assunto") or "").strip()

            save_handoff_lead(
                client_id=client_id,
                agent_id=agent_id,
                instance=instance,
                from_number=from_number,
                nome=nome,
                telefone=telefone,
                assunto=assunto,
            )
            state["lead_saved"] = True
    except Exception as e:
        logger.error("CHATLAB_LEAD_SAVE_ERROR: client_id=%s agent_id=%s instance=%s err=%s", client_id, agent_id, instance, e)

    return JSONResponse({"ok": True, "reply": reply, "paused": False, "state": state})


# -----------------------------------------------------------------------------
# Monitor (NOC)
# -----------------------------------------------------------------------------
@router.get("/monitor", name="admin_web_monitor")
async def monitor_page(req: Request):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Faça login para acessar.")

    flash = _flash_from_query(req)
    mode = (req.query_params.get("mode") or "").strip().lower()

    ctx = {
        "request": req,
        "active_nav": "monitor",
        "flash": flash,
        "mode": mode,
        "tv_mode": (mode == "tv"),
        "data_url": "/admin/web/monitor/data",  # recomendo fixo/relativo
    }
    return templates.TemplateResponse("monitor.html", ctx)


@router.get("/monitor/data", name="admin_web_monitor_data")
async def monitor_data(req: Request):
    try:
        _require_admin(req)
    except PermissionError:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    with SessionLocal() as db:
        agents = db.execute(select(Agent).order_by(desc(Agent.created_at))).scalars().all()

        out = []
        online = degraded = offline = unknown = 0
        latencies: list[int] = []

        for a in agents:
            last = (
                db.execute(
                    select(AgentCheck)
                    .where(AgentCheck.agent_id == a.id)
                    .order_by(desc(AgentCheck.checked_at))
                    .limit(1)
                )
                .scalar_one_or_none()
            )

            status = (getattr(last, "status", None) or "unknown").lower()
            latency_ms = getattr(last, "latency_ms", None)
            err = getattr(last, "error", None)
            checked_at = getattr(last, "checked_at", None)

            if status == "online":
                online += 1
            elif status == "degraded":
                degraded += 1
            elif status == "offline":
                offline += 1
            else:
                unknown += 1

            if isinstance(latency_ms, int):
                latencies.append(latency_ms)

            out.append(
                {
                    "agent_id": a.id,
                    "client_id": a.client_id,
                    "name": a.name,
                    "instance": a.instance,
                    "configured": bool((a.evolution_base_url or "").strip()),
                    "last_seen_at": _fmt_dt_br(getattr(a, "last_seen_at", None)) if getattr(a, "last_seen_at", None) else None,
                    "status": status,
                    "latency_ms": latency_ms,
                    "error": err,
                    "checked_at": _fmt_dt_br(checked_at) if checked_at else None,
                }
            )

        avg_latency = int(sum(latencies) / len(latencies)) if latencies else None

    return JSONResponse(
        {
            "ok": True,
            "stats": {
                "online": online,
                "degraded": degraded,
                "offline": offline,
                "unknown": unknown,
                "avg_latency_ms": avg_latency,
            },
            "items": out,
        }
    )


# -----------------------------------------------------------------------------
# Agent Rules Editor
# -----------------------------------------------------------------------------
@router.get("/agents/{agent_id}/rules", name="admin_web_agent_rules")
async def agent_rules_page(req: Request, agent_id: str):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Faça login para acessar.")

    flash = _flash_from_query(req)
    agent_id = (agent_id or "").strip()

    with SessionLocal() as db:
        a = db.execute(select(Agent).where(Agent.id == agent_id).limit(1)).scalar_one_or_none()
        if not a:
            return _redirect(req, "admin_web_agents", flash_kind="error", flash_message=f"Agent não encontrado: {agent_id}")

        rules_obj = getattr(a, "rules_json", None) or {}
        rules_text = json.dumps(rules_obj, ensure_ascii=False, indent=2)

        client = db.execute(select(Client).where(Client.id == a.client_id).limit(1)).scalar_one_or_none()
        client_name = getattr(client, "name", None) if client else None

    ctx = {
        "request": req,
        "active_nav": "agents",
        "flash": flash,
        "agent": {
            "id": a.id,
            "client_id": a.client_id,
            "client_name": client_name or a.client_id,
            "name": a.name,
            "instance": a.instance,
            "status": a.status,
        },
        "rules_text": rules_text,
        "save_action": _url(req, "admin_web_agent_rules_save", agent_id=agent_id),
        "reset_action": _url(req, "admin_web_agent_rules_reset", agent_id=agent_id),
        "back_url": _url(req, "admin_web_agents"),
    }
    return templates.TemplateResponse("agent_rules.html", ctx)


@router.post("/agents/{agent_id}/rules/save", name="admin_web_agent_rules_save")
async def agent_rules_save(req: Request, agent_id: str, rules_text: str = Form("")):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Faça login para acessar.")

    agent_id = (agent_id or "").strip()
    rules_text = (rules_text or "").strip()

    if not rules_text:
        return _redirect(
            req,
            "admin_web_agent_rules",
            flash_kind="error",
            flash_message="Cole um JSON válido (não pode vazio).",
            path_params={"agent_id": agent_id},
        )

    try:
        parsed = json.loads(rules_text)
        if not isinstance(parsed, dict):
            return _redirect(
                req,
                "admin_web_agent_rules",
                flash_kind="error",
                flash_message="O JSON raiz deve ser um objeto (dict).",
                path_params={"agent_id": agent_id},
            )
    except Exception as e:
        return _redirect(
            req,
            "admin_web_agent_rules",
            flash_kind="error",
            flash_message=f"JSON inválido: {e}",
            path_params={"agent_id": agent_id},
        )

    with SessionLocal() as db:
        a = db.execute(select(Agent).where(Agent.id == agent_id).limit(1)).scalar_one_or_none()
        if not a:
            return _redirect(req, "admin_web_agents", flash_kind="error", flash_message=f"Agent não encontrado: {agent_id}")

        a.rules_json = parsed  # JSONB
        try:
            a.rules_updated_at = func.now()
        except Exception:
            pass

        db.add(a)
        db.commit()
        db.refresh(a)

    # invalida cache do rules_engine
    try:
        invalidate_agent_rules(agent_id)
    except Exception:
        pass

    logger.info("ADMIN_WEB_RULES_SAVED: agent_id=%s instance=%s", agent_id, getattr(a, "instance", None))
    return _redirect(
        req,
        "admin_web_agent_rules",
        flash_kind="success",
        flash_message="Regras salvas com sucesso.",
        path_params={"agent_id": agent_id},
    )


@router.post("/agents/{agent_id}/rules/reset", name="admin_web_agent_rules_reset")
async def agent_rules_reset(req: Request, agent_id: str):
    """
    Reseta rules_json para um template básico (bootstrap rápido).
    """
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Faça login para acessar.")

    agent_id = (agent_id or "").strip()

    template_rules = {
        "branding": {"name": "Atendimento"},
        "hours": {"mode": "business", "open": "08:00", "close": "18:00"},
        "messages": {
            "off_hours": "Estamos fora do horário agora 🙂. Se quiser atendimento, digite *atendente*.",
            "welcome": "Olá! Digite *menu* para ver opções.",
            "fallback": "Não entendi. Digite *menu* para ver opções.",
            "handoff_prompt": "Perfeito! Para encaminhar para um atendente, envie:\n*Nome* - *Telefone* - *Assunto*",
            "handoff_ok": "Obrigado! ✅ Recebemos suas informações e um atendente vai falar com você em breve.",
            "handoff_retry": "Não consegui entender. Envie no formato:\n*Nome* - *Telefone* - *Assunto*",
        },
        "menu": {
            "title": "Menu Principal",
            "options": [
                {"key": "1", "label": "Vendas", "reply": "Certo! Me diga o que você precisa em Vendas."},
                {"key": "2", "label": "Suporte", "reply": "Beleza! Me diga qual o problema."},
            ],
        },
        "handoff": {"keyword": "atendente", "capture_lead": True},
    }

    with SessionLocal() as db:
        a = db.execute(select(Agent).where(Agent.id == agent_id).limit(1)).scalar_one_or_none()
        if not a:
            return _redirect(req, "admin_web_agents", flash_kind="error", flash_message=f"Agent não encontrado: {agent_id}")

        a.rules_json = template_rules
        try:
            a.rules_updated_at = func.now()
        except Exception:
            pass

        db.add(a)
        db.commit()
        db.refresh(a)

    try:
        invalidate_agent_rules(agent_id)
    except Exception:
        pass

    logger.info("ADMIN_WEB_RULES_RESET: agent_id=%s instance=%s", agent_id, getattr(a, "instance", None))
    return _redirect(
        req,
        "admin_web_agent_rules",
        flash_kind="success",
        flash_message="Regras resetadas para o template básico.",
        path_params={"agent_id": agent_id},
    )
