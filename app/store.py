import time

class MemoryStore:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MemoryStore, cls).__new__(cls)
            cls._instance.seen_ids = set()
            cls._instance.user_state = {}
        return cls._instance

    def seen(self, message_id: str):
        if not message_id:
            return False
        if message_id in self.seen_ids:
            return True
        self.seen_ids.add(message_id)
        return False

    def _normalize_number(self, number: str) -> str:
        # Remove tudo que não é dígito
        digits = "".join(c for c in (number or "") if c.isdigit())
        # Para o Brasil (55), o número pode vir com ou sem o 9 extra.
        # Pegar os últimos 10 dígitos (DDD + 8 números) costuma ser o mais estável para bater.
        if len(digits) >= 10:
            return digits[-10:]
        return digits

    def get_state(self, number: str):
        key = self._normalize_number(number)
        return self.user_state.setdefault(key, {})

    def set_paused(self, number: str, seconds: int):
        state = self.get_state(number)
        state["bot_paused_until"] = int(time.time()) + int(seconds)

    def is_paused(self, number: str) -> bool:
        state = self.get_state(number)
        until = int(state.get("bot_paused_until") or 0)
        return int(time.time()) < until
