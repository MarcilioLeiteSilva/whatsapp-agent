"""
app/evolution.py

Cliente de envio para Evolution API.

Modo SaaS (multiagente):
- Cada agente tem seu próprio:
  - evolution_base_url
  - api_key
  - instance
- O main.py resolve o Agent pelo instance do webhook e chama send_text()
  passando as credenciais/config daquele agente.

Observação:
- Mantemos também suporte a "default" via settings, como fallback opcional,
  mas o caminho certo é o per-agent.
"""

import httpx

from .settings import EVOLUTION_BASE_URL, EVOLUTION_INSTANCE, EVOLUTION_TOKEN


def _normalize_base_url(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    return base


def _validate_base_url(base: str) -> None:
    if not base:
        raise ValueError("Evolution base_url vazio")
    if not (base.startswith("http://") or base.startswith("https://")):
        raise ValueError(f"Evolution base_url inválida (sem protocolo): {base!r}")


class EvolutionClient:
    """
    Mantém defaults (via settings.py), mas no modo SaaS a chamada deve passar
    os parâmetros por agente.

    Exemplo SaaS:
        await evo.send_text(
            number,
            reply,
            base_url=agent.evolution_base_url,
            instance=agent.instance,
            api_key=agent.api_key,
        )
    """

    def __init__(self):
        # Defaults (compat). No SaaS, prefira passar via parâmetros.
        self.base = _normalize_base_url(EVOLUTION_BASE_URL)
        self.instance = (EVOLUTION_INSTANCE or "").strip()
        self.token = (EVOLUTION_TOKEN or "").strip()

    @staticmethod
    def _build_headers(api_key: str) -> dict:
        headers: dict = {}
        tok = (api_key or "").strip()
        if tok:
            # Evolution geralmente usa 'apikey'
            headers["apikey"] = tok
            # Alguns setups também aceitam Bearer; não atrapalha
            headers["Authorization"] = f"Bearer {tok}"
        return headers

    async def send_text(
        self,
        number: str,
        text: str,
        *,
        base_url: str | None = None,
        instance: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int = 20,
    ):
        """
        Envia texto para um número WhatsApp via Evolution.

        Preferência de resolução (SaaS):
        - base_url: parâmetro (agent.evolution_base_url) > default settings
        - instance: parâmetro (agent.instance) > default settings
        - api_key: parâmetro (agent.api_key) > default settings
        """
        base = _normalize_base_url(base_url or self.base)
        inst = (instance or self.instance or "").strip()
        tok = (api_key or self.token or "").strip()

        _validate_base_url(base)
        if not inst:
            raise ValueError("Evolution instance vazio (agent.instance ou EVOLUTION_INSTANCE)")
        if not tok:
            raise ValueError("Evolution api_key/token vazio (agent.api_key ou EVOLUTION_TOKEN)")

        url = f"{base}/message/sendText/{inst}"
        payload = {"number": number, "text": text}
        headers = self._build_headers(tok)

        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            return r.json()
