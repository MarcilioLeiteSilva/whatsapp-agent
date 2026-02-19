import os
from dotenv import load_dotenv

load_dotenv()

EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "")
EVOLUTION_TOKEN = os.getenv("EVOLUTION_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
