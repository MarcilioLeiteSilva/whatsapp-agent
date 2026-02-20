import time
from collections import defaultdict, deque

class RateLimiter:
    """
    Limite simples por número: N mensagens por janela (segundos).
    """
    def __init__(self, max_events: int = 8, window_seconds: int = 10):
        self.max_events = max_events
        self.window = window_seconds
        self.events = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.time()
        q = self.events[key]
        # remove eventos antigos
        while q and (now - q[0]) > self.window:
            q.popleft()
        if len(q) >= self.max_events:
            return False
        q.append(now)
        return True
