import os
from dotenv import load_dotenv

load_dotenv()

EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "")
EVOLUTION_TOKEN = os.getenv("EVOLUTION_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
# FLUXO 1: ERP -> AGENTE (Administração/QR/Status) - MANTIDO NOME ORIGINAL
INTEGRATION_KEY = os.getenv("WHATSAPP_AGENT_KEY") or os.getenv("INTEGRATION_KEY") or "Cascavel_KLv5f9og5"

# FLUXO 2: AGENTE -> ERP (Entrega de Acerto/Webhook)
CONSIGO_WEBHOOK_KEY = os.getenv("CONSIGO_WEBHOOK_KEY", "consigo_inventory_secret")
AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", "") # URL deste serviço para a Evolution
CONSIGO_WEBHOOK_URL = os.getenv("CONSIGO_WEBHOOK_URL", "") # URL da plataforma Consigo
