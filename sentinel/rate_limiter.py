"""In-memory monotonic sliding-window admission limits for dry-run requests."""

import asyncio
import math
import time
from collections import deque


class RateLimiter:
    def __init__(self, *, global_limit: int, group_limit: int, window_seconds: float,
                 clock=time.monotonic):
        if any(type(value) is not int or value <= 0 for value in (global_limit, group_limit)):
            raise ValueError("limits must be positive integers")
        if type(window_seconds) not in (int, float) or not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("invalid rate window")
        self.global_limit = global_limit
        self.group_limit = group_limit
        self.window = window_seconds
        self.clock = clock
        self.global_events = deque()
        self.group_events = {}
        self.lock = asyncio.Lock()

    async def allow(self, platform_id: str, group_id: str) -> bool:
        async with self.lock:
            now = self.clock()
            cutoff = now - self.window
            while self.global_events and self.global_events[0][0] <= cutoff:
                _, expired_key = self.global_events.popleft()
                events = self.group_events[expired_key]
                events.popleft()
                if not events:
                    del self.group_events[expired_key]
            key = (platform_id, group_id)
            events = self.group_events.get(key)
            if len(self.global_events) >= self.global_limit or (events and len(events) >= self.group_limit):
                return False
            self.global_events.append((now, key))
            self.group_events.setdefault(key, deque()).append(now)
            return True
