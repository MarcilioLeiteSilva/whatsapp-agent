import logging
from fastapi import APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
from .evolution import EvolutionClient
from .store import MemoryStore
from .settings import INTEGRATION_KEY
from .db import SessionLocal
from .models import Agent

router = APIRouter(prefix="/v1/integration")
logger = logging.getLogger("agent")

async def verify_key(
    x_key: Optional[str] = Header(None, alias="x-integration-key"),
    auth: Optional[str] = Header(None, alias="Authorization")
):
    from .settings import INTEGRATION_API_KEY
    
    # Tenta pegar do x-integration-key ou do Bearer token
    provided_key = x_key
    if not provided_key and auth and auth.startswith("Bearer "):
        provided_key = auth.replace("Bearer ", "")
        
    if provided_key != INTEGRATION_API_KEY:
        logger.warning(f"403 Forbidden: Management key mismatch. Provided: {provided_key}")
        raise HTTPException(status_code=403, detail="Invalid integration key")

class InventoryItem(BaseModel):
    lot_id: str
    product_name: str
    expected_quantity: int

class InventoryStart(BaseModel):
    instance_name: str
    pdv_phone: str
    closing_id: int
    message: str
    items: Optional[List[InventoryItem]] = []

class InstanceCreate(BaseModel):
    client_id: str
    client_name: str
    instance_name: str

@router.post("/instances")
async def create_instance(data: InstanceCreate, _ = Depends(verify_key)):
    logger.info(f"CREATE_INSTANCE: {data.instance_name}")
    evo = EvolutionClient()
    try:
        # Tenta criar a instância na Evolution
        try:
            await evo.create_instance(data.instance_name)
        except Exception as e:
            logger.warning(f"Instance already exists or creation notice: {e}")
        
        # Vincula no banco de dados local
        with SessionLocal() as db:
            from sqlalchemy import select
            agent = db.execute(select(Agent).where(Agent.instance == data.instance_name)).scalar_one_or_none()
            if not agent:
                agent = Agent(
                    id=data.instance_name,
                    client_id=data.client_id,
                    instance=data.instance_name,
                    name=f"Agente {data.client_name}"
                )
                db.add(agent)
                db.commit()
        
        return {"ok": True, "instance": data.instance_name}
    except Exception as e:
        logger.error(f"ERROR_CREATE_INSTANCE: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/instances/{name}/status")
async def get_status(name: str, _ = Depends(verify_key)):
    evo = EvolutionClient()
    try:
        # Retorna o objeto BRUTO da Evolution para a Consigo
        res = await evo.get_connection_state(name)
        return res
    except Exception as e:
        return {"instance": {"state": "ERROR", "error": str(e)}}

@router.get("/instances/{name}/qr")
async def get_qr(name: str, _ = Depends(verify_key)):
    evo = EvolutionClient()
    try:
        # Retorna o objeto BRUTO (com base64 e code) da Evolution para a Consigo
        res = await evo.get_qr_code(name)
        return res
    except Exception as e:
        logger.error(f"ERROR_GET_QR: {e}")
        return {"ok": False, "error": str(e)}

@router.delete("/instances/{name}")
async def delete_instance(name: str, _ = Depends(verify_key)):
    logger.info(f"DELETE_INSTANCE: {name}")
    evo = EvolutionClient()
    try:
        try:
            await evo.logout_instance(name)
        except: pass
        await evo.delete_instance(name)
        with SessionLocal() as db:
            from sqlalchemy import delete
            db.execute(delete(Agent).where(Agent.instance == name))
            db.commit()
        return {"ok": True}
    except Exception as e:
        logger.error(f"ERROR_DELETE_INSTANCE: {e}")
        return {"ok": False, "error": str(e)}

@router.post("/agents/inventory/start")
async def start_inventory(data: InventoryStart, _ = Depends(verify_key)):
    logger.info(f"START_INVENTORY: pdv={data.pdv_phone}")
    evo = EvolutionClient()
    store = MemoryStore()
    
    state = store.get_state(data.pdv_phone)
    state.clear()
    state["step"] = "inventory_pending"
    state["closing_id"] = data.closing_id
    state["inventory_items"] = [item.dict() for item in (data.items or [])]
    state["notified_consigo"] = False
    
    # Acorda o robô
    store.set_paused(data.pdv_phone, 0)
    
    store.save_state(data.pdv_phone, state)
    await evo.send_text(data.instance_name, data.pdv_phone, data.message)
    
    return {"ok": True}
