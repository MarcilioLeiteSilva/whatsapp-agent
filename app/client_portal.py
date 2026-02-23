# app/client_portal.py
from __future__ import annotations

import os
import logging
from typing import Optional, Any

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from sqlalchemy import select, func, desc
from .db import SessionLocal
from .models import Client, Agent, Lead

logger = logging.getLogger("agent")

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
router = APIRouter(prefix="/portal", tags=["client_portal"])

COOKIE_CLIENT_ID = "client_id"
COOKIE_CLIENT_TOKEN = "client_token"


def _url(req: Request, name: str, **path_params: Any) -> str:
    return str(req.url_for(name, **path_params))


def _redirect(req: Request, to_name: str, *, flash_kind: str = "info", flash_message: str = "") -> RedirectResponse:
    url = _url(req, to_name)
    if flash_message:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}flash_kind={flash_kind}&flash_message={flash_message}"
    return RedirectResponse(url, status_code=303)


def _flash_from_query(req: Request) -> Optional[dict]:
    kind = (req.query_params.get("flash_kind") or "").strip()
    msg = (req.query_params.get("flash_message") or "").strip()
    if not msg:
        return None
    return {"kind": kind or "info", "message": msg}


def _require_client(req: Request) -> Client:
    client_id = (req.cookies.get(COOKIE_CLIENT_ID) or "").strip()
    token = (req.cookies.get(COOKIE_CLIENT_TOKEN) or "").strip()

    if not client_id or not token:
        raise PermissionError("unauthorized")

    with SessionLocal() as db:
        c = db.execute(select(Client).where(Client.id == client_id).limit(1)).scalar_one_or_none()
        if not c or not (c.login_token or "").strip() or (c.login_token or "").strip() != token:
            raise PermissionError("unauthorized")

        try:
            c.login_token_last_used_at = func.now()
            db.add(c)
            db.commit()
        except Exception:
            pass

        return c


@router.get("/login", name="portal_login")
async def portal_login_page(req: Request):
    flash = _flash_from_query(req)
    ctx = {"request": req, "flash": flash, "login_action": _url(req, "portal_login_post")}
    return templates.TemplateResponse("portal_login.html", ctx)


@router.post("/login", name="portal_login_post")
async def portal_login_post(req: Request, client_id: str = Form(""), token: str = Form("")):
    client_id = (client_id or "").strip()
    token = (token or "").strip()

    if not client_id or not token:
        return _redirect(req, "portal_login", flash_kind="error", flash_message="Informe Client ID e Token.")

    with SessionLocal() as db:
        c = db.execute(select(Client).where(Client.id == client_id).limit(1)).scalar_one_or_none()
        if not c or (c.login_token or "").strip() != token:
            return _redirect(req, "portal_login", flash_kind="error", flash_message="Client ID ou token inválidos.")

    resp = _redirect(req, "portal_dashboard", flash_kind="success", flash_message="Login realizado.")
    resp.set_cookie(COOKIE_CLIENT_ID, client_id, httponly=True, secure=True, samesite="lax", max_age=60 * 60 * 12)
    resp.set_cookie(COOKIE_CLIENT_TOKEN, token, httponly=True, secure=True, samesite="lax", max_age=60 * 60 * 12)
    return resp


@router.get("/logout", name="portal_logout")
async def portal_logout(req: Request):
    resp = _redirect(req, "portal_login", flash_kind="success", flash_message="Você saiu.")
    resp.delete_cookie(COOKIE_CLIENT_ID)
    resp.delete_cookie(COOKIE_CLIENT_TOKEN)
    return resp


@router.get("", name="portal_dashboard")
async def portal_dashboard(req: Request):
    try:
        c = _require_client(req)
    except PermissionError:
        return _redirect(req, "portal_login", flash_kind="error", flash_message="Faça login.")

    flash = _flash_from_query(req)

    with SessionLocal() as db:
        agents = db.execute(
            select(Agent).where(Agent.client_id == c.id).order_by(desc(Agent.created_at))
        ).scalars().all()

        total_leads = db.execute(
            select(func.count()).select_from(Lead).where(Lead.client_id == c.id)
        ).scalar_one()

        rows = db.execute(
            select(Lead.agent_id, func.count())
            .where(Lead.client_id == c.id)
            .group_by(Lead.agent_id)
        ).all()

        name_map = {str(a.id): str(getattr(a, "name", "") or a.id) for a in agents}
        leads_by_agent = []
        for agent_id, cnt in rows:
            aid = str(agent_id) if agent_id else ""
            leads_by_agent.append({
                "agent_id": aid or None,
                "agent_name": name_map.get(aid, "—") if aid else "—",
                "count": int(cnt or 0),
            })
        leads_by_agent.sort(key=lambda x: x["count"], reverse=True)

        recent = db.execute(
            select(Lead).where(Lead.client_id == c.id).order_by(desc(Lead.created_at)).limit(20)
        ).scalars().all()

        # map agent_name no recent
        recent_view = []
        for l in recent:
            aid = str(getattr(l, "agent_id", "") or "")
            recent_view.append({
                "created_at": str(getattr(l, "created_at", "") or "") or None,
                "agent_name": name_map.get(aid, "—") if aid else "—",
                "instance": getattr(l, "instance", None),
                "from_number": getattr(l, "from_number", None),
                "nome": getattr(l, "nome", None),
                "telefone": getattr(l, "telefone", None),
                "assunto": getattr(l, "assunto", None),
                "status": getattr(l, "status", None),
            })

    ctx = {
        "request": req,
        "flash": flash,
        "active_nav": "dashboard",
        "client": {"id": c.id, "name": c.name, "plan": c.plan},
        "stats": {"total_leads": int(total_leads or 0), "agents_count": len(agents)},
        "leads_by_agent": leads_by_agent,
        "recent_leads": recent_view,
    }
    return templates.TemplateResponse("portal_dashboard.html", ctx)


@router.get("/agents", name="portal_agents")
async def portal_agents(req: Request):
    try:
        c = _require_client(req)
    except PermissionError:
        return _redirect(req, "portal_login", flash_kind="error", flash_message="Faça login.")

    flash = _flash_from_query(req)

    with SessionLocal() as db:
        agents = db.execute(
            select(Agent).where(Agent.client_id == c.id).order_by(desc(Agent.created_at))
        ).scalars().all()

    ctx = {
        "request": req,
        "flash": flash,
        "active_nav": "agents",
        "client": {"id": c.id, "name": c.name},
        "agents": agents,
    }
    return templates.TemplateResponse("portal_agents.html", ctx)


@router.get("/leads", name="portal_leads")
async def portal_leads(req: Request, agent_id: str = "", q: str = ""):
    try:
        c = _require_client(req)
    except PermissionError:
        return _redirect(req, "portal_login", flash_kind="error", flash_message="Faça login.")

    flash = _flash_from_query(req)
    agent_id = (agent_id or "").strip()
    q = (q or "").strip()

    with SessionLocal() as db:
        agents = db.execute(
            select(Agent).where(Agent.client_id == c.id).order_by(desc(Agent.created_at))
        ).scalars().all()

        stmt = select(Lead).where(Lead.client_id == c.id)

        if agent_id:
            stmt = stmt.where(Lead.agent_id == agent_id)

        if q:
            like = f"%{q}%"
            stmt = stmt.where(
                (Lead.from_number.ilike(like)) |
                (Lead.nome.ilike(like)) |
                (Lead.telefone.ilike(like)) |
                (Lead.assunto.ilike(like)) |
                (Lead.instance.ilike(like))
            )

        leads = db.execute(stmt.order_by(desc(Lead.created_at)).limit(200)).scalars().all()
        name_map = {str(a.id): str(getattr(a, "name", "") or a.id) for a in agents}

        leads_view = []
        for l in leads:
            aid = str(getattr(l, "agent_id", "") or "")
            leads_view.append({
                "created_at": str(getattr(l, "created_at", "") or "") or None,
                "agent_id": getattr(l, "agent_id", None),
                "agent_name": name_map.get(aid, "—") if aid else "—",
                "instance": getattr(l, "instance", None),
                "from_number": getattr(l, "from_number", None),
                "nome": getattr(l, "nome", None),
                "telefone": getattr(l, "telefone", None),
                "assunto": getattr(l, "assunto", None),
                "intent_detected": getattr(l, "intent_detected", None),
                "status": getattr(l, "status", None),
            })

    ctx = {
        "request": req,
        "flash": flash,
        "active_nav": "leads",
        "client": {"id": c.id, "name": c.name},
        "agents": agents,
        "agent_id": agent_id,
        "q": q,
        "leads": leads_view,
    }
    return templates.TemplateResponse("portal_leads.html", ctx)
