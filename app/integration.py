import logging
from fastapi import APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
from .evolution import EvolutionClient
from .store import MemoryStore
from .settings import INTEGRATION_KEY

router = APIRouter(prefix="/v1/integration")
logger = logging.getLogger("agent")

async def verify_key(x_integration_key: str = Header(None)):
    if INTEGRATION_KEY and x_integration_key != INTEGRATION_KEY:
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

@router.post("/agents/inventory/start")
async def start_inventory(data: InventoryStart, _ = Depends(verify_key)):
    logger.info(f"START_INVENTORY: pdv={data.pdv_phone}")
    evo = EvolutionClient()
    store = MemoryStore()
    
    state = store.get_state(data.pdv_phone)
    state["step"] = "inventory_pending"
    state["closing_id"] = data.closing_id
    state["inventory_items"] = [item.dict() for item in (data.items or [])]
    state["notified_consigo"] = False
    
    store.save_state(data.pdv_phone, state)
    await evo.send_text(data.instance_name, data.pdv_phone, data.message)
    
    return {"ok": True}
