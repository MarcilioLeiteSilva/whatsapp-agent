# app/store.py
from __future__ import annotations

import time
from typing import Dict, Any, Optional


class MemoryStore:
    """
    Armazenamento em memória simples (process-local).

    Responsável por:
    - Deduplicação de message_id
    - Estado da conversa por número (ou chave customizada)
    - Controle de pausa do bot

    Observação:
    - É memória em RAM (reinicia ao reiniciar o container/processo).
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

    def touch_state(self, key: str) -> None:
        """
        Garante que o estado exista (sem alterar conteúdo).
        Útil para debug/telemetria.
        """
        _ = self.get_state(key)

    # ---------------------------------------------------------
    # Pausa do bot
    # ---------------------------------------------------------
    def set_paused(self, key: str, seconds: int) -> None:
        """
        Pausa por X segundos.
        """
        state = self.get_state(key)
        state["bot_paused_until"] = int(time.time()) + int(seconds)
        state.pop("bot_paused_forever", None)  # se estava em forever, remove
        self.set_state(key, state)

    def pause_forever(self, key: str) -> None:
        """
        Pausa "até liberar".
        Útil após handoff humano (bot não responde mais).
        """
        state = self.get_state(key)
        state["bot_paused_forever"] = True
        state.pop("bot_paused_until", None)
        self.set_state(key, state)

    def clear_paused(self, key: str) -> None:
        """
        Remove a pausa (reativa o bot).
        """
        state = self.get_state(key)
        state.pop("bot_paused_until", None)
        state.pop("bot_paused_forever", None)
        self.set_state(key, state)

    def get_paused_until(self, key: str) -> Optional[int]:
        """
        Retorna timestamp unix (int) do fim da pausa, ou None.
        Se estiver em pausa forever, retorna -1.
        """
        state = self.get_state(key)
        if state.get("bot_paused_forever"):
            return -1
        until = state.get("bot_paused_until")
        return int(until) if until else None

    def is_paused(self, key: str) -> bool:
        """
        Retorna True se o bot deve ficar inativo para esta conversa.
        """
        state = self.get_state(key)

        if state.get("bot_paused_forever"):
            return True

        until = int(state.get("bot_paused_until") or 0)
        return int(time.time()) < until
