import time
import json
import logging
from sqlalchemy.orm import Session
from .db import SessionLocal
from .models import ConversationState
from sqlalchemy import select

logger = logging.getLogger("agent")

class MemoryStore:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MemoryStore, cls).__new__(cls)
            cls._instance.seen_ids = set()
        return cls._instance

    def _normalize_number(self, number: str) -> str:
        digits = "".join(c for c in (number or "") if c.isdigit())
        if len(digits) >= 8:
            return digits[-8:]
        return digits

    def seen(self, message_id: str):
        if not message_id:
            return False
        if message_id in self.seen_ids:
            return True
        self.seen_ids.add(message_id)
        return False

    def get_state(self, number: str):
        key = self._normalize_number(number)
        with SessionLocal() as db:
            row = db.execute(select(ConversationState).where(ConversationState.id == key)).scalar_one_or_none()
            if row:
                return row.state_json or {}
            return {}

    def save_state(self, number: str, state: dict):
        key = self._normalize_number(number)
        with SessionLocal() as db:
            row = db.execute(select(ConversationState).where(ConversationState.id == key)).scalar_one_or_none()
            if not row:
                row = ConversationState(id=key)
                db.add(row)
            
            row.state_json = state
            db.commit()

    def set_paused(self, number: str, seconds: int):
        state = self.get_state(number)
        state["bot_paused_until"] = int(time.time()) + int(seconds)
        self.save_state(number, state)

    def is_paused(self, number: str) -> bool:
        state = self.get_state(number)
        until = int(state.get("bot_paused_until") or 0)
        return int(time.time()) < until
