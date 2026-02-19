import time

class MemoryStore:
    def __init__(self):
        self.seen_ids = set()
        self.user_state = {}  # number -> dict

    def seen(self, message_id: str):
        if not message_id:
            return False
        if message_id in self.seen_ids:
            return True
        self.seen_ids.add(message_id)
        return False

    def get_state(self, number: str):
        return self.user_state.setdefault(number, {})

    def set_paused(self, number: str, seconds: int):
        state = self.get_state(number)
        state["bot_paused_until"] = int(time.time()) + int(seconds)

    def is_paused(self, number: str) -> bool:
        state = self.get_state(number)
        until = int(state.get("bot_paused_until") or 0)
        return int(time.time()) < until
