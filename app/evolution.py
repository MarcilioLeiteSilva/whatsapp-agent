import httpx
from .settings import EVOLUTION_BASE_URL, EVOLUTION_INSTANCE, EVOLUTION_TOKEN

class EvolutionClient:
    def __init__(self):
        self.base = EVOLUTION_BASE_URL.rstrip("/")
        self.instance = EVOLUTION_INSTANCE
        self.headers = {}

        # Evolution geralmente usa 'apikey'
        if EVOLUTION_TOKEN:
            self.headers["apikey"] = EVOLUTION_TOKEN
            # alguns setups também aceitam Bearer; não atrapalha
            self.headers["Authorization"] = f"Bearer {EVOLUTION_TOKEN}"

    async def send_text(self, number: str, text: str):
        url = f"{self.base}/message/sendText/{self.instance}"
        payload = {"number": number, "text": text}

        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, json=payload, headers=self.headers)
            r.raise_for_status()
            return r.json()
