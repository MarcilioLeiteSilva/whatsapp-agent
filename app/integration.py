import logging
from typing import Optional
from fastapi import APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel

from .evolution import EvolutionClient
from .db import SessionLocal
from .models import Agent, Client
from .settings import INTEGRATION_KEY, AGENT_BASE_URL
from sqlalchemy import select

logger = logging.getLogger("agent")
router = APIRouter(prefix="/v1/integration", tags=["integration"])
evo = EvolutionClient()

# --- Schemas ---
class InstanceCreate(BaseModel):
    client_id: str
    client_name: str
    instance_name: str

class InstanceResponse(BaseModel):
    instance: str
    status: str
    hash: Optional[str] = None
    qrcode: Optional[str] = None

class InventoryStart(BaseModel):
    instance_name: str
    pdv_phone: str
    closing_id: int
    message: str

# --- Auth ---
async def verify_key(x_integration_key: str = Header(...)):
    if x_integration_key != INTEGRATION_KEY:
        raise HTTPException(status_code=403, detail="Invalid Integration Key")
    return x_integration_key

# --- Endpoints ---

@router.get("/instances")
async def list_instances(client_id: Optional[str] = None, _ = Depends(verify_key)):
    """Lista as instâncias cadastradas."""
    with SessionLocal() as db:
        q = select(Agent)
        if client_id:
            q = q.where(Agent.client_id == client_id)
        agents = db.execute(q).scalars().all()
        return agents

@router.post("/instances", response_model=InstanceResponse)
async def create_instance(data: InstanceCreate, _ = Depends(verify_key)):
    """Cria uma nova instância na Evolution e registra no banco local."""
    
    with SessionLocal() as db:
        # 1. Garantir que o cliente existe
        client = db.execute(select(Client).where(Client.id == data.client_id)).scalar_one_or_none()
        if not client:
            client = Client(id=data.client_id, name=data.client_name)
            db.add(client)
            db.commit()

        # 2. Verificar se a instância já existe no DB
        existing = db.execute(select(Agent).where(Agent.instance == data.instance_name)).scalar_one_or_none()
        if existing:
            raise HTTPException(status_code=400, detail="Instance name already exists in database")

        # 3. Criar na Evolution API
        try:
            res = await evo.create_instance(data.instance_name)
            # Evolution v2 retorna dados da instância
            instance_data = res.get("instance", res)
            
            # 4. Registrar Agente no DB
            new_agent = Agent(
                id=f"ag_{data.instance_name}",
                client_id=data.client_id,
                name=data.instance_name,
                instance=data.instance_name,
                status="created"
            )
            db.add(new_agent)
            db.commit()

            # 5. Configurar Webhook automaticamente
            if AGENT_BASE_URL:
                webhook_url = f"{AGENT_BASE_URL.rstrip('/')}/webhook"
                await evo.set_webhook(data.instance_name, webhook_url)
                logger.info(f"Webhook set for {data.instance_name}: {webhook_url}")

            return {
                "instance": data.instance_name,
                "status": "created",
                "qrcode": res.get("qrcode", {}).get("base64") if isinstance(res.get("qrcode"), dict) else None
            }
        except Exception as e:
            logger.error(f"Error creating instance: {e}")
            raise HTTPException(status_code=500, detail=str(e))

@router.get("/instances/{instance}/qr")
async def get_qr(instance: str, _ = Depends(verify_key)):
    """Busca o QR Code atual para conexão."""
    try:
        res = await evo.get_qr_code(instance)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/instances/{instance}/status")
async def get_status(instance: str, _ = Depends(verify_key)):
    """Verifica o status da conexão na Evolution."""
    try:
        res = await evo.get_connection_state(instance)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/instances/{instance}")
async def delete_instance(instance: str, _ = Depends(verify_key)):
    """Remove a instância da Evolution e do banco local."""
    try:
        # Tenta deletar na Evolution (ignorando erro se não existir lá)
        try:
            await evo.delete_instance(instance)
        except:
            pass

        # Remove do banco local
        with SessionLocal() as db:
            agent = db.execute(select(Agent).where(Agent.instance == instance)).scalar_one_or_none()
            if agent:
                db.delete(agent)
                db.commit()
        
        return {"ok": True, "message": f"Instance {instance} deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/agents/inventory/start")
async def start_inventory(data: InventoryStart, _ = Depends(verify_key)):
    """Inicia ativamente uma conversa de acerto com um PDV."""
    from .store import MemoryStore
    store = MemoryStore()
    
    try:
        # 1. Configurar estado da conversa para o número
        state = store.get_state(data.pdv_phone)
        state["step"] = "inventory_pending"
        state["closing_id"] = data.closing_id
        
        # 2. Enviar mensagem inicial via Evolution
        await evo.send_text(data.instance_name, data.pdv_phone, data.message)
        
        logger.info(f"Inventory started for {data.pdv_phone} on instance {data.instance_name}")
        return {"ok": True, "message": "Inventory flow started"}
    except Exception as e:
        logger.error(f"Error starting inventory: {e}")
        raise HTTPException(status_code=500, detail=str(e))
