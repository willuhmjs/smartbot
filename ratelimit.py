"""Rate limits for members who aren't staff: generous enough that someone chatting never notices, tight
enough that one account (or a handful) can't run up the bot's model usage."""

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self):
        self.member_hits: dict[tuple[int, int], deque] = defaultdict(deque)
        self.server_hits: dict[int, deque] = defaultdict(deque)
        self._notified: dict[tuple[int, int], float] = {}

    @staticmethod
    def _count(hits: deque, window: float, now: float) -> int:
        while hits and hits[0] <= now - 3600:
            hits.popleft()
        return sum(1 for t in hits if t > now - window)

    def check(self, guild_id: int, user_id: int, per_minute: int, per_hour: int, server_per_hour: int,
              now: float | None = None) -> float:
        """Records a request and returns 0 if it's allowed, or else how many seconds until it would be.
        A limit of 0 means no limit."""
        now = time.monotonic() if now is None else now
        mine, server = self.member_hits[(guild_id, user_id)], self.server_hits[guild_id]
        waits = []
        for hits, limit, window in ((mine, per_minute, 60), (mine, per_hour, 3600), (server, server_per_hour, 3600)):
            if limit and self._count(hits, window, now) >= limit:
                recent = [t for t in hits if t > now - window]
                waits.append(recent[-limit] + window - now)
        if waits:
            return max(1.0, max(waits))
        mine.append(now)
        server.append(now)
        return 0.0

    def should_notify(self, guild_id: int, user_id: int, wait: float, now: float | None = None) -> bool:
        """Tell a limited member once per limit, not on every message (that would be spam of its own)."""
        now = time.monotonic() if now is None else now
        key = (guild_id, user_id)
        if self._notified.get(key, 0) > now:
            return False
        self._notified[key] = now + wait
        return True
