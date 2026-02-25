# app/admin_plans.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .db import SessionLocal
from .models import Plan, PlanFeature

router = APIRouter(prefix="/admin/plans", tags=["admin:plans"])


@router.get("")
def list_plans() -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.execute(select(Plan).order_by(Plan.id.asc())).scalars().all()
    return [{"id": p.id, "name": p.name, "is_active": bool(p.is_active)} for p in rows]


@router.post("")
def create_plan(payload: Dict[str, Any]) -> Dict[str, Any]:
    plan_id = (payload.get("id") or "").strip()
    name = (payload.get("name") or "").strip()
    if not plan_id or not name:
        raise HTTPException(status_code=400, detail="id and name are required")

    with SessionLocal() as db:
        exists = db.execute(select(Plan).where(Plan.id == plan_id)).scalar_one_or_none()
        if exists:
            raise HTTPException(status_code=409, detail="plan already exists")

        db.add(Plan(id=plan_id, name=name, is_active=bool(payload.get("is_active", True))))
        db.commit()

    return {"ok": True, "id": plan_id}


@router.put("/{plan_id}")
def update_plan(plan_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    with SessionLocal() as db:
        plan = db.execute(select(Plan).where(Plan.id == plan_id)).scalar_one_or_none()
        if not plan:
            raise HTTPException(status_code=404, detail="plan not found")

        if "name" in payload:
            plan.name = (payload.get("name") or "").strip() or plan.name
        if "is_active" in payload:
            plan.is_active = bool(payload.get("is_active"))

        db.commit()
    return {"ok": True}


@router.get("/{plan_id}/features")
def get_features(plan_id: str) -> Dict[str, Any]:
    with SessionLocal() as db:
        rows = db.execute(select(PlanFeature).where(PlanFeature.plan_id == plan_id)).scalars().all()
    return {"plan_id": plan_id, "features": {r.key: r.value_json for r in rows}}


@router.put("/{plan_id}/features")
def upsert_features(plan_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    payload:
      { "features": { "ai_enabled": true, "max_agents": 10, ... } }
    """
    features = payload.get("features")
    if not isinstance(features, dict):
        raise HTTPException(status_code=400, detail="features must be an object")

    with SessionLocal() as db:
        # garante que o plano existe
        plan = db.execute(select(Plan).where(Plan.id == plan_id)).scalar_one_or_none()
        if not plan:
            raise HTTPException(status_code=404, detail="plan not found")

        for k, v in features.items():
            key = str(k).strip()
            if not key:
                continue

            stmt = insert(PlanFeature).values(
                plan_id=plan_id,
                key=key,
                value_json=v,
            ).on_conflict_do_update(
                index_elements=["plan_id", "key"],
                set_={"value_json": v},
            )
            db.execute(stmt)

        db.commit()

    return {"ok": True}
