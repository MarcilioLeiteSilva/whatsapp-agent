import os
from dotenv import load_dotenv

load_dotenv()

EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "")
EVOLUTION_TOKEN = os.getenv("EVOLUTION_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
# Chave de segurança para falar com a Consigo (Aceita a nova, a do painel ou a padrão)
INTEGRATION_KEY = os.getenv("CONSIGO_WEBHOOK_KEY") or os.getenv("WHATSAPP_AGENT_KEY") or "Cascavel_KLv5f9og5"
AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", "") # URL deste serviço para a Evolution
CONSIGO_WEBHOOK_URL = os.getenv("CONSIGO_WEBHOOK_URL", "") # URL da plataforma Consigo
