from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert

from .db import SessionLocal
from .models import Plan, PlanFeature
from .admin_web import templates  # <- reaproveita o Jinja2Templates do admin_web.py


router = APIRouter()


def _parse_features_from_form(form: Dict[str, Any]) -> Dict[str, Any]:
    keys = form.getlist("feature_key")
    vals = form.getlist("feature_value")
    out: Dict[str, Any] = {}

    for k, v in zip(keys, vals):
        key = (k or "").strip()
        val_raw = (v or "").strip()
        if not key:
            continue
        if not val_raw:
            continue

        # value precisa ser JSON válido (true/false/10/{"x":1}/"texto")
        try:
            out[key] = json.loads(val_raw)
        except Exception:
            # fallback: tratar como string (mantém funcionamento, evita perder edição)
            out[key] = val_raw

    return out


@router.get("/admin/plans", name="admin_web_plans")
def admin_web_plans(request: Request):
    with SessionLocal() as db:
        plans = db.execute(select(Plan).order_by(Plan.id.asc())).scalars().all()

        # contar features por plano
        counts = db.execute(
            select(PlanFeature.plan_id, func.count().label("c"))
            .group_by(PlanFeature.plan_id)
        ).all()
        count_map = {pid: int(c) for pid, c in counts}

    view = []
    for p in plans:
        view.append({
            "id": p.id,
            "name": p.name,
            "is_active": bool(p.is_active),
            "features_count": count_map.get(p.id, 0),
        })

    return templates.TemplateResponse(
        "plans/index.html",
        {
            "request": request,
            "active_nav": "plans",
            "plans": view,
        },
    )


@router.get("/admin/plans/new", name="admin_web_plans_new")
def admin_web_plans_new(request: Request):
    return templates.TemplateResponse(
        "plans/form.html",
        {
            "request": request,
            "active_nav": "plans",
            "is_edit": False,
            "plan": None,
            "features": {},
        },
    )


@router.post("/admin/plans/new", name="admin_web_plans_create")
async def admin_web_plans_create(request: Request):
    form = await request.form()
    plan_id = (form.get("id") or "").strip()
    name = (form.get("name") or "").strip()
    is_active = bool(form.get("is_active"))

    features = _parse_features_from_form(form)

    with SessionLocal() as db:
        db.add(Plan(id=plan_id, name=name, is_active=is_active))
        db.commit()

        # upsert features
        for k, v in features.items():
            stmt = insert(PlanFeature).values(
                plan_id=plan_id,
                key=k,
                value_json=v,
                updated_at=datetime.now(timezone.utc),
            ).on_conflict_do_update(
                index_elements=["plan_id", "key"],
                set_={"value_json": v, "updated_at": datetime.now(timezone.utc)},
            )
            db.execute(stmt)

        db.commit()

    return RedirectResponse(url="/admin/plans", status_code=303)


@router.get("/admin/plans/{plan_id}/edit", name="admin_web_plans_edit")
def admin_web_plans_edit(plan_id: str, request: Request):
    with SessionLocal() as db:
        plan = db.execute(select(Plan).where(Plan.id == plan_id)).scalar_one()
        rows = db.execute(select(PlanFeature).where(PlanFeature.plan_id == plan_id)).scalars().all()
        features = {r.key: r.value_json for r in rows}

    return templates.TemplateResponse(
        "plans/form.html",
        {
            "request": request,
            "active_nav": "plans",
            "is_edit": True,
            "plan": plan,
            "features": features,
        },
    )


@router.post("/admin/plans/{plan_id}/edit", name="admin_web_plans_update")
async def admin_web_plans_update(plan_id: str, request: Request):
    form = await request.form()
    name = (form.get("name") or "").strip()
    is_active = bool(form.get("is_active"))
    features = _parse_features_from_form(form)

    with SessionLocal() as db:
        plan = db.execute(select(Plan).where(Plan.id == plan_id)).scalar_one()
        plan.name = name or plan.name
        plan.is_active = is_active
        plan.updated_at = datetime.now(timezone.utc)
        db.commit()

        # upsert features do form (não apaga outras automaticamente)
        for k, v in features.items():
            stmt = insert(PlanFeature).values(
                plan_id=plan_id,
                key=k,
                value_json=v,
                updated_at=datetime.now(timezone.utc),
            ).on_conflict_do_update(
                index_elements=["plan_id", "key"],
                set_={"value_json": v, "updated_at": datetime.now(timezone.utc)},
            )
            db.execute(stmt)

        db.commit()

    return RedirectResponse(url="/admin/plans", status_code=303)


@router.post("/admin/plans/{plan_id}/toggle", name="admin_web_plans_toggle")
def admin_web_plans_toggle(plan_id: str):
    with SessionLocal() as db:
        plan = db.execute(select(Plan).where(Plan.id == plan_id)).scalar_one()
        plan.is_active = not bool(plan.is_active)
        plan.updated_at = datetime.now(timezone.utc)
        db.commit()

    return RedirectResponse(url="/admin/plans", status_code=303)
