import unittest

from sentinel.circuit_breaker import CircuitBreaker


class BreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_trip_probe_and_recovery(self):
        now = [0.0]
        breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=5, clock=lambda: now[0])
        first, second, stale = await breaker.acquire(), await breaker.acquire(), await breaker.acquire()
        await breaker.complete(first, success=False)
        await breaker.complete(second, success=False)
        await breaker.complete(stale, success=True)
        self.assertIsNone(await breaker.acquire())
        now[0] = 5
        probe = await breaker.acquire()
        self.assertIsNotNone(probe)
        self.assertIsNone(await breaker.acquire())
        await breaker.complete(probe, success=False)
        self.assertIsNone(await breaker.acquire())
        now[0] = 10
        probe = await breaker.acquire()
        await breaker.complete(probe, success=True)
        self.assertIsNotNone(await breaker.acquire())

    async def test_probe_without_model_call_does_not_close_circuit(self):
        now = [0.0]
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=5, clock=lambda: now[0])
        await breaker.complete(await breaker.acquire(), success=False)
        now[0] = 5
        await breaker.abandon(await breaker.acquire())
        self.assertIsNotNone(await breaker.acquire())
        self.assertIsNotNone(breaker.opened_at)

    async def test_invalid_settings(self):
        for kwargs in ({"failure_threshold": True, "cooldown_seconds": 1},
                       {"failure_threshold": 1, "cooldown_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                CircuitBreaker(**kwargs)
