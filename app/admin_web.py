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
ONLINE_SECONDS = int(os.getenv("AGENT_ONLINE_SECONDS", "120"))  # last_seen_at <= 120s => online

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
    return f"{prefix}_{secrets.token_hex(6)}"


def _normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def _require_admin(req: Request) -> None:
    if not ADMIN_TOKEN:
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
    db_ok = True
    db_err = None
    try:
        with SessionLocal() as db:
            _ = db.execute(select(func.count()).select_from(Lead)).scalar_one()
    except Exception as e:
        db_ok = False
        db_err = str(e)

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
    ctx = {"request": req, "active_nav": "", "flash": flash, "login_action": _url(req, "admin_web_login_post")}
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
        max_age=60 * 60 * 12,
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


@router.get("/clients", name="admin_web_clients")
async def clients_page(req: Request):
    try:
        _require_admin(req)
    except PermissionError:
        return _redirect(req, "admin_web_login", flash_kind="error", flash_message="Faça login para acessar.")

    flash = _flash_from_query(req)

    with SessionLocal() as db:
        clients = db.execute(select(Client).order_by(desc(Client.created_at))).scalars().all()

    # ✅ IMPORTANTE: não gerar URL de rota que exige client_id aqui
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
# (resto do arquivo permanece igual ao seu)
# - agents, leads, export.csv, simulator, chatlab, monitor, rules editor...
# -----------------------------------------------------------------------------

# ⚠️ Para manter a resposta enxuta, eu não repliquei aqui o restante que você já tem
# (ele não tem relação com o bug). Se você quiser, eu te devolvo o arquivo 100% inteiro.
