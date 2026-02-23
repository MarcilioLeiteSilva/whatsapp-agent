"""
app/portal_web.py

Portal do Cliente (SSR) - cliente enxerga APENAS dados do próprio client_id.

Rotas:
- Login:       /portal/login
- Logout:      /portal/logout
- Dashboard:   /portal
- Agents:      /portal/agents
- Leads:       /portal/leads
- Export CSV:  /portal/leads/export.csv

Auth:
- Login: client_id + token (clients.login_token)
- Cookie httponly: client_token + client_id

Timezone/format:
- APP_TIMEZONE=America/Sao_Paulo (default)
- Datas: dd/mm/yyyy HH:MM:SS

Online badge:
- AGENT_ONLINE_WINDOW_SECONDS=300 (default 5 min)
"""

from __future__ import annotations

import os
import csv
import io
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Any

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.templating import Jinja2Templates

from sqlalchemy import select, func, desc, or_

from .db import SessionLocal
from .models import Client, Agent, Lead

logger = logging.getLogger("agent")

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
router = APIRouter(prefix="/portal", tags=["portal_web"])

BR_TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "America/Sao_Paulo"))
ONLINE_WINDOW_SECONDS = int(os.getenv("AGENT_ONLINE_WINDOW_SECONDS", "300"))  # 5 min


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
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
        from urllib.parse import urlencode

        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urlencode(qp)}"

    return RedirectResponse(url, status_code=303)


def _to_local_dt(dt: Any) -> Optional[datetime]:
    if not dt:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BR_TZ)
    return None


def _fmt_dt_br(dt: Any) -> Optional[str]:
    d = _to_local_dt(dt)
    if not d:
        return None
    return d.strftime("%d/%m/%Y %H:%M:%S")


def _is_agent_online(last_seen_at: Any) -> bool:
    d = _to_local_dt(last_seen_at)
    if not d:
        return False
    now = datetime.now(BR_TZ)
    return (now - d) <= timedelta(seconds=ONLINE_WINDOW_SECONDS)


def _require_client(req: Request) -> str:
    """
    Auth do portal:
    - cookie client_id + client_token
    """
    client_id = (req.cookies.get("client_id") or "").strip()
    token = (req.cookies.get("client_token") or "").strip()

    if not client_id or not token:
        raise PermissionError("unauthorized")

    with SessionLocal() as db:
        c = db.execute(select(Client).where(Client.id == client_id).limit(1)).scalar_one_or_none()
        if not c:
            raise PermissionError("unauthorized")

        db_token = (getattr(c, "login_token", None) or "").strip()
        if not db_token or db_token != token:
            raise PermissionError("unauthorized")

    return client_id


def _client_to_view(c: Client) -> dict:
    return {
        "id": getattr(c, "id", None),
        "name": getattr(c, "name", None),
        "plan": getattr(c, "plan", None),
    }


def _agent_to_view(a: Agent) -> dict:
    last_seen_raw = getattr(a, "last_seen_at", None)
    return {
        "id": getattr(a, "id", None),
        "client_id": getattr(a, "client_id", None),
        "name": getattr(a, "name", None),
        "instance": getattr(a, "instance", None),
        "status": getattr(a, "status", None),
        "online": _is_agent_online(last_seen_raw),
        "last_seen_at": _fmt_dt_br(last_seen_raw) or None,
        "created_at": _fmt_dt_br(getattr(a, "created_at", None)) or None,
    }


def _lead_to_view(l: Lead, agent_name: Optional[str] = None) -> dict:
    return {
        "id": getattr(l, "id", None),
        "client_id": getattr(l, "client_id", None),
        "agent_id": getattr(l, "agent_id", None),
        "agent_name": agent_name,
        "instance": getattr(l, "instance", None),
        "from_number": getattr(l, "from_number", None),
        "nome": getattr(l, "nome", None),
        "telefone": getattr(l, "telefone", None),
        "assunto": getattr(l, "assunto", None),
        "status": getattr(l, "status", None),
        "intent_detected": getattr(l, "intent_detected", None),
        "created_at": _fmt_dt_br(getattr(l, "created_at", None)) or None,
    }


def _build_portal_leads_stmt(client_id: str, q: str = "", agent_id: str = "", limit: int = 200):
    q = (q or "").strip()
    agent_id = (agent_id or "").strip()

    stmt = select(Lead).where(Lead.client_id == client_id)

    if agent_id:
        stmt = stmt.where(Lead.agent_id == agent_id)

    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Lead.from_number.ilike(like),
                Lead.nome.ilike(like),
                Lead.telefone.ilike(like),
                Lead.assunto.ilike(like),
                Lead.instance.ilike(like),
                Lead.agent_id.ilike(like),
            )
        )

    stmt = stmt.order_by(desc(Lead.created_at)).limit(limit)
    return stmt


# -----------------------------------------------------------------------------
# Auth
# -----------------------------------------------------------------------------
@router.get("/login", name="portal_login")
async def portal_login_page(req: Request):
    flash = _flash_from_query(req)
    ctx = {
        "request": req,
        "flash": flash,
        "login_action": _url(req, "portal_login_post"),
    }
    return templates.TemplateResponse("portal_login.html", ctx)


@router.post("/login", name="portal_login_post")
async def portal_login_post(req: Request, client_id: str = Form(""), token: str = Form("")):
    client_id = (client_id or "").strip()
    token = (token or "").strip()

    if not client_id or not token:
        return _redirect(req, "portal_login", flash_kind="error", flash_message="Informe client_id e token.")

    with SessionLocal() as db:
        c = db.execute(select(Client).where(Client.id == client_id).limit(1)).scalar_one_or_none()
        if not c:
            return _redirect(req, "portal_login", flash_kind="error", flash_message="Client não encontrado.")

        db_token = (getattr(c, "login_token", None) or "").strip()
        if not db_token:
            return _redirect(req, "portal_login", flash_kind="error", flash_message="Client sem token. Peça ao admin gerar.")
        if db_token != token:
            return _redirect(req, "portal_login", flash_kind="error", flash_message="Token inválido.")

        # marca last used (best effort)
        try:
            c.login_token_last_used_at = func.now()
            db.add(c)
            db.commit()
        except Exception:
            pass

        client_name = getattr(c, "name", None) or client_id

    resp = _redirect(req, "portal_dashboard", flash_kind="success", flash_message=f"Bem-vindo(a), {client_name}!")

    # cookie do portal (separado do admin)
    resp.set_cookie(
        key="client_id",
        value=client_id,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=60 * 60 * 12,  # 12h
    )
    resp.set_cookie(
        key="client_token",
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=60 * 60 * 12,  # 12h
    )
    return resp


@router.get("/logout", name="portal_logout")
async def portal_logout(req: Request):
    resp = _redirect(req, "portal_login", flash_kind="success", flash_message="Você saiu do portal.")
    resp.delete_cookie("client_id")
    resp.delete_cookie("client_token")
    return resp


# -----------------------------------------------------------------------------
# Pages (protected)
# -----------------------------------------------------------------------------
@router.get("", name="portal_dashboard")
async def portal_dashboard(req: Request):
    try:
        client_id = _require_client(req)
    except PermissionError:
        return _redirect(req, "portal_login", flash_kind="error", flash_message="Faça login para acessar.")

    flash = _flash_from_query(req)

    with SessionLocal() as db:
        client = db.execute(select(Client).where(Client.id == client_id).limit(1)).scalar_one_or_none()
        client_view = _client_to_view(client) if client else {"id": client_id, "name": client_id, "plan": ""}

        total_leads = db.execute(
            select(func.count()).select_from(Lead).where(Lead.client_id == client_id)
        ).scalar_one()

        agents = db.execute(
            select(Agent).where(Agent.client_id == client_id).order_by(desc(Agent.created_at))
        ).scalars().all()

        agents_map = {str(a.id): str(getattr(a, "name", "") or a.id) for a in agents}
        agent_ids = [a.id for a in agents]

        per_agent_rows = []
        if agent_ids:
            per_agent_rows = db.execute(
                select(Lead.agent_id, func.count().label("cnt"))
                .where(Lead.client_id == client_id)
                .group_by(Lead.agent_id)
            ).all()

        per_agent = []
        null_cnt = db.execute(
            select(func.count()).select_from(Lead).where(Lead.client_id == client_id, Lead.agent_id.is_(None))
        ).scalar_one()
        if null_cnt:
            per_agent.append({"agent_id": None, "agent_name": "— (sem agente)", "count": int(null_cnt)})

        for agent_id_val, cnt in per_agent_rows:
            if agent_id_val is None:
                continue
            per_agent.append({
                "agent_id": str(agent_id_val),
                "agent_name": agents_map.get(str(agent_id_val), str(agent_id_val)),
                "count": int(cnt or 0),
            })

        per_agent.sort(key=lambda x: x["count"], reverse=True)

        recent = db.execute(
            select(Lead).where(Lead.client_id == client_id).order_by(desc(Lead.created_at)).limit(10)
        ).scalars().all()

        recent_view = [
            _lead_to_view(l, agent_name=agents_map.get(str(getattr(l, "agent_id", "") or "")))
            for l in recent
        ]

    ctx = {
        "request": req,
        "flash": flash,
        "active_nav": "dashboard",
        "client": client_view,
        "stats": {"total_leads": int(total_leads or 0), "agents_count": len(agents)},
        "per_agent": per_agent,
        "recent_leads": recent_view,
    }
    return templates.TemplateResponse("portal_dashboard.html", ctx)


@router.get("/agents", name="portal_agents")
async def portal_agents(req: Request):
    try:
        client_id = _require_client(req)
    except PermissionError:
        return _redirect(req, "portal_login", flash_kind="error", flash_message="Faça login para acessar.")

    flash = _flash_from_query(req)

    with SessionLocal() as db:
        client = db.execute(select(Client).where(Client.id == client_id).limit(1)).scalar_one_or_none()
        client_view = _client_to_view(client) if client else {"id": client_id, "name": client_id, "plan": ""}

        agents = db.execute(
            select(Agent).where(Agent.client_id == client_id).order_by(desc(Agent.created_at))
        ).scalars().all()

    ctx = {
        "request": req,
        "flash": flash,
        "active_nav": "agents",
        "client": client_view,
        "agents": [_agent_to_view(a) for a in agents],
    }
    return templates.TemplateResponse("portal_agents.html", ctx)


@router.get("/leads", name="portal_leads")
async def portal_leads(req: Request, q: str = "", agent_id: str = ""):
    try:
        client_id = _require_client(req)
    except PermissionError:
        return _redirect(req, "portal_login", flash_kind="error", flash_message="Faça login para acessar.")

    flash = _flash_from_query(req)
    q = (q or "").strip()
    agent_id = (agent_id or "").strip()

    with SessionLocal() as db:
        client = db.execute(select(Client).where(Client.id == client_id).limit(1)).scalar_one_or_none()
        client_view = _client_to_view(client) if client else {"id": client_id, "name": client_id, "plan": ""}

        agents = db.execute(
            select(Agent).where(Agent.client_id == client_id).order_by(desc(Agent.created_at))
        ).scalars().all()
        agents_map = {str(a.id): str(getattr(a, "name", "") or a.id) for a in agents}

        stmt = _build_portal_leads_stmt(client_id=client_id, q=q, agent_id=agent_id, limit=200)
        leads = db.execute(stmt).scalars().all()

        leads_view = [
            _lead_to_view(l, agent_name=agents_map.get(str(getattr(l, "agent_id", "") or "")))
            for l in leads
        ]

    ctx = {
        "request": req,
        "flash": flash,
        "active_nav": "leads",
        "client": client_view,
        "q": q,
        "agent_id": agent_id,
        "agents": [{"id": str(a.id), "name": str(getattr(a, "name", "") or a.id)} for a in agents],
        "leads": leads_view,
        "export_csv_url": _url(req, "portal_leads_export_csv"),  # pra você usar no template
    }
    return templates.TemplateResponse("portal_leads.html", ctx)


# -----------------------------------------------------------------------------
# Export CSV (protected)
# -----------------------------------------------------------------------------
@router.get("/leads/export.csv", name="portal_leads_export_csv")
async def portal_leads_export_csv(req: Request, q: str = "", agent_id: str = ""):
    try:
        client_id = _require_client(req)
    except PermissionError:
        return _redirect(req, "portal_login", flash_kind="error", flash_message="Faça login para acessar.")

    q = (q or "").strip()
    agent_id = (agent_id or "").strip()

    with SessionLocal() as db:
        agents = db.execute(
            select(Agent).where(Agent.client_id == client_id)
        ).scalars().all()
        agents_map = {str(a.id): str(getattr(a, "name", "") or a.id) for a in agents}

        stmt = _build_portal_leads_stmt(client_id=client_id, q=q, agent_id=agent_id, limit=5000)
        leads = db.execute(stmt).scalars().all()

        rows = [
            _lead_to_view(l, agent_name=agents_map.get(str(getattr(l, "agent_id", "") or "")))
            for l in leads
        ]

    def _iter_csv():
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")

        w.writerow([
            "id", "created_at", "agent_id", "agent_name",
            "instance", "from_number", "nome", "telefone",
            "assunto", "intent_detected", "status",
        ])

        for r in rows:
            w.writerow([
                r.get("id"),
                r.get("created_at"),
                r.get("agent_id"),
                r.get("agent_name"),
                r.get("instance"),
                r.get("from_number"),
                r.get("nome"),
                r.get("telefone"),
                r.get("assunto"),
                r.get("intent_detected"),
                r.get("status"),
            ])

        yield buf.getvalue()

    filename = f"leads_{client_id}_{datetime.now(BR_TZ).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        _iter_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
