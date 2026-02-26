# app/admin_web_plans.py
from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert

from .db import SessionLocal
from .models import Plan, PlanFeature

# ✅ cria templates aqui (mesmo diretório do admin_web.py)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

router = APIRouter()

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _parse_features_from_form(form) -> Dict[str, Any]:
    """
    Recebe FormData (Starlette). Espera arrays:
      feature_key, feature_value
    value precisa ser JSON válido (true/false/10/{"x":1}/"texto")
    """
    keys = form.getlist("feature_key")
    vals = form.getlist("feature_value")

    out: Dict[str, Any] = {}
    for k, v in zip(keys, vals):
        key = (k or "").strip()
        val_raw = (v or "").strip()
        if not key or not val_raw:
            continue

        try:
            out[key] = json.loads(val_raw)
        except Exception:
            # fallback: salva como string
            out[key] = val_raw
    return out


# -----------------------------------------------------------------------------
# Pages
# -----------------------------------------------------------------------------
@router.get("/plans", name="admin_web_plans")
async def plans_index(req: Request):
    with SessionLocal() as db:
        plans = db.execute(select(Plan).order_by(Plan.id.asc())).scalars().all()

        counts = db.execute(
            select(PlanFeature.plan_id, func.count().label("c"))
            .group_by(PlanFeature.plan_id)
        ).all()
        count_map = {pid: int(c) for pid, c in counts}

    view = [{
        "id": p.id,
        "name": p.name,
        "is_active": bool(p.is_active),
        "features_count": count_map.get(p.id, 0),
    } for p in plans]

    return templates.TemplateResponse(
        "plans/index.html",
        {"request": req, "active_nav": "plans", "plans": view},
    )


@router.get("/plans/new", name="admin_web_plans_new")
async def plans_new(req: Request):
    return templates.TemplateResponse(
        "plans/form.html",
        {"request": req, "active_nav": "plans", "is_edit": False, "plan": None, "features": {}},
    )


@router.post("/plans/new", name="admin_web_plans_create")
async def plans_create(req: Request):
    form = await req.form()

    plan_id = (form.get("id") or "").strip()
    name = (form.get("name") or "").strip()
    is_active = bool(form.get("is_active"))

    if not plan_id or not name:
        # simples: volta com flash via query (se quiser, integro no seu _redirect depois)
        return RedirectResponse(url="/admin/web/plans?flash_kind=error&flash_message=ID+e+Nome+s%C3%A3o+obrigat%C3%B3rios", status_code=303)

    features = _parse_features_from_form(form)

    with SessionLocal() as db:
        db.add(Plan(id=plan_id, name=name, is_active=is_active))
        db.commit()

        now = datetime.now(timezone.utc)
        for k, v in features.items():
            stmt = insert(PlanFeature).values(
                plan_id=plan_id,
                key=k,
                value_json=v,
                updated_at=now,
            ).on_conflict_do_update(
                index_elements=["plan_id", "key"],
                set_={"value_json": v, "updated_at": now},
            )
            db.execute(stmt)

        db.commit()

    return RedirectResponse(url="/admin/web/plans?flash_kind=success&flash_message=Plano+criado", status_code=303)


@router.get("/plans/{plan_id}/edit", name="admin_web_plans_edit")
async def plans_edit(plan_id: str, req: Request):
    with SessionLocal() as db:
        plan = db.execute(select(Plan).where(Plan.id == plan_id)).scalar_one_or_none()
        if not plan:
            return RedirectResponse(url="/admin/web/plans?flash_kind=error&flash_message=Plano+n%C3%A3o+encontrado", status_code=303)

        rows = db.execute(select(PlanFeature).where(PlanFeature.plan_id == plan_id)).scalars().all()
        features = {r.key: r.value_json for r in rows}

    return templates.TemplateResponse(
        "plans/form.html",
        {"request": req, "active_nav": "plans", "is_edit": True, "plan": plan, "features": features},
    )


@router.post("/plans/{plan_id}/edit", name="admin_web_plans_update")
async def plans_update(plan_id: str, req: Request):
    form = await req.form()
    name = (form.get("name") or "").strip()
    is_active = bool(form.get("is_active"))
    features = _parse_features_from_form(form)

    with SessionLocal() as db:
        plan = db.execute(select(Plan).where(Plan.id == plan_id)).scalar_one_or_none()
        if not plan:
            return RedirectResponse(url="/admin/web/plans?flash_kind=error&flash_message=Plano+n%C3%A3o+encontrado", status_code=303)

        if name:
            plan.name = name
        plan.is_active = is_active
        plan.updated_at = datetime.now(timezone.utc)
        db.commit()

        # upsert features enviadas
        now = datetime.now(timezone.utc)
        for k, v in features.items():
            stmt = insert(PlanFeature).values(
                plan_id=plan_id,
                key=k,
                value_json=v,
                updated_at=now,
            ).on_conflict_do_update(
                index_elements=["plan_id", "key"],
                set_={"value_json": v, "updated_at": now},
            )
            db.execute(stmt)

        db.commit()

    return RedirectResponse(url="/admin/web/plans?flash_kind=success&flash_message=Plano+salvo", status_code=303)


@router.post("/plans/{plan_id}/toggle", name="admin_web_plans_toggle")
async def plans_toggle(plan_id: str):
    with SessionLocal() as db:
        plan = db.execute(select(Plan).where(Plan.id == plan_id)).scalar_one_or_none()
        if not plan:
            return RedirectResponse(url="/admin/web/plans?flash_kind=error&flash_message=Plano+n%C3%A3o+encontrado", status_code=303)

        plan.is_active = not bool(plan.is_active)
        plan.updated_at = datetime.now(timezone.utc)
        db.commit()

    return RedirectResponse(url="/admin/web/plans?flash_kind=success&flash_message=Status+alterado", status_code=303)
