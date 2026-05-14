import httpx
import logging
from .settings import EVOLUTION_BASE_URL, EVOLUTION_TOKEN

logger = logging.getLogger("agent")

class EvolutionClient:
    def __init__(self):
        self.base = EVOLUTION_BASE_URL.rstrip("/")
        self.headers = {}
        if EVOLUTION_TOKEN:
            self.headers["apikey"] = EVOLUTION_TOKEN

    async def _request(self, method: str, path: str, json=None, params=None):
        url = f"{self.base}{path}"
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                r = await client.request(method, url, json=json, params=params, headers=self.headers)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                logger.error(f"Evolution API Error [{method} {path}]: {e}")
                raise

    async def send_text(self, instance: str, number: str, text: str):
        path = f"/message/sendText/{instance}"
        payload = {"number": number, "text": text}
        return await self._request("POST", path, json=payload)

    async def create_instance(self, instance_name: str):
        path = "/instance/create"
        payload = {
            "instanceName": instance_name,
            "qrcode": True
        }
        # Nota: Algumas versões da Evolution v2 exigem que não envie o campo 'token' se quiser que ele gere um automático.
        return await self._request("POST", path, json=payload)

    async def set_webhook(self, instance: str, webhook_url: str):
        path = f"/webhook/set/{instance}"
        payload = {
            "enabled": True,
            "url": webhook_url,
            "webhook_by_events": False,
            "events": [
                "MESSAGES_UPSERT",
                "MESSAGES_UPDATE",
                "MESSAGES_DELETE",
                "SEND_MESSAGE",
                "CONNECTION_UPDATE"
            ]
        }
        return await self._request("POST", path, json=payload)

    async def get_qr_code(self, instance: str):
        path = f"/instance/connect/{instance}"
        return await self._request("GET", path)

    async def get_connection_state(self, instance: str):
        path = f"/instance/connectionState/{instance}"
        return await self._request("GET", path)

    async def logout_instance(self, instance: str):
        path = f"/instance/logout/{instance}"
        return await self._request("DELETE", path)

    async def delete_instance(self, instance: str):
        path = f"/instance/delete/{instance}"
        return await self._request("DELETE", path)
