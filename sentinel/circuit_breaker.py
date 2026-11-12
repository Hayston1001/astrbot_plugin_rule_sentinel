"""Async model-failure circuit breaker with a single recovery probe."""

import asyncio
import math
import time


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int, cooldown_seconds: float, clock=time.monotonic):
        if type(failure_threshold) is not int or failure_threshold <= 0:
            raise ValueError("invalid failure threshold")
        if type(cooldown_seconds) not in (int, float) or not math.isfinite(cooldown_seconds) or cooldown_seconds <= 0:
            raise ValueError("invalid cooldown")
        self.threshold = failure_threshold
        self.cooldown = cooldown_seconds
        self.clock = clock
        self.lock = asyncio.Lock()
        self.failures = 0
        self.opened_at = None
        self.probing = False
        self.generation = 0

    async def acquire(self):
        """Return a generation token, or None if requests must be skipped."""
        async with self.lock:
            if self.opened_at is None:
                return (self.generation, False)
            if self.clock() - self.opened_at < self.cooldown or self.probing:
                return None
            self.probing = True
            return (self.generation, True)

    async def complete(self, token, *, success: bool):
        """Ignore stale completions from requests started before a trip."""
        async with self.lock:
            generation, probe = token
            if generation != self.generation:
                return
            if probe:
                self.probing = False
                if success:
                    self.failures = 0
                    self.opened_at = None
                else:
                    self.opened_at = self.clock()
                self.generation += 1
            elif self.opened_at is None:
                if success:
                    self.failures = 0
                else:
                    self.failures += 1
                    if self.failures >= self.threshold:
                        self.opened_at = self.clock()
                        self.generation += 1

    async def abandon(self, token):
        """Release a probe that did not actually reach the model."""
        async with self.lock:
            generation, probe = token
            if generation == self.generation and probe:
                self.probing = False
