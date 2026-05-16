import logging
from fastapi import APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
from .evolution import EvolutionClient
from .store import MemoryStore

router = APIRouter(prefix="/v1/integration")
logger = logging.getLogger("agent")

# Chave simples de segurança para a integração
from .settings import INTEGRATION_KEY

async def verify_key(x_integration_key: str = Header(None)):
    if not INTEGRATION_KEY:
        return # Se não configurado, ignora (dev)
    if x_integration_key != INTEGRATION_KEY:
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
    """
    Endpoint chamado pela Consigo para disparar o robô de acerto.
    """
    logger.info(f"START_INVENTORY: pdv={data.pdv_phone} items={len(data.items or [])}")
    
    evo = EvolutionClient()
    store = MemoryStore()
    
    try:
        # 1. Preparar o estado (Sessão ATIVA e LIMPA)
        state = store.get_state(data.pdv_phone)
        
        # Resetamos apenas o essencial para a nova sessão de acerto
        state.clear()
        state["status"] = "active"
        state["step"] = "inventory_pending"
        state["notified_consigo"] = False
        state["closing_id"] = data.closing_id
        state["inventory_items"] = [item.dict() for item in (data.items or [])]
        
        # Salva no Banco de Dados
        store.save_state(data.pdv_phone, state)
        
        # 2. Enviar mensagem inicial via Evolution
        await evo.send_text(data.instance_name, data.pdv_phone, data.message)
        
        return {"ok": True, "message": "Inventory flow started"}
    except Exception as e:
        logger.error(f"ERROR_START_INVENTORY: {e}")
        raise HTTPException(status_code=500, detail=str(e))
