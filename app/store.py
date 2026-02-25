import time
from typing import Dict, Any


class MemoryStore:
    """
    Armazenamento em memória simples (process-local).

    Responsável por:
    - Deduplicação de message_id
    - Estado da conversa por número (ou chave customizada)
    - Controle de pausa do bot
    """

    def __init__(self):
        self.seen_ids = set()
        self.user_state: Dict[str, Dict[str, Any]] = {}

    # ---------------------------------------------------------
    # Deduplicação
    # ---------------------------------------------------------
    def seen(self, message_id: str) -> bool:
        """
        Retorna True se já viu esse message_id.
        """
        if not message_id:
            return False

        if message_id in self.seen_ids:
            return True

        self.seen_ids.add(message_id)
        return False

    # ---------------------------------------------------------
    # Estado da conversa
    # ---------------------------------------------------------
    def get_state(self, key: str) -> Dict[str, Any]:
        """
        Retorna o estado atual da conversa.
        Sempre garante um dict mutável.
        """
        return self.user_state.setdefault(key, {})

    def set_state(self, key: str, state: Dict[str, Any]) -> None:
        """
        Persiste explicitamente o estado da conversa.
        (Necessário para fluxos que mutam o dict fora da store.)
        """
        if not isinstance(state, dict):
            state = {}
        self.user_state[key] = state

    def clear_state(self, key: str) -> None:
        """
        Remove completamente o estado da conversa.
        """
        if key in self.user_state:
            del self.user_state[key]

    # ---------------------------------------------------------
    # Pausa do bot
    # ---------------------------------------------------------
    def set_paused(self, key: str, seconds: int) -> None:
        state = self.get_state(key)
        state["bot_paused_until"] = int(time.time()) + int(seconds)
        self.set_state(key, state)

    def is_paused(self, key: str) -> bool:
        state = self.get_state(key)
        until = int(state.get("bot_paused_until") or 0)
        return int(time.time()) < until
