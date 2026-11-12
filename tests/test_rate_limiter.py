import unittest

from sentinel.rate_limiter import RateLimiter


class RateLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_group_and_expiration(self):
        now = [0.0]
        limiter = RateLimiter(global_limit=2, group_limit=1, window_seconds=5, clock=lambda: now[0])
        self.assertTrue(await limiter.allow("p", "a"))
        self.assertFalse(await limiter.allow("p", "a"))
        self.assertTrue(await limiter.allow("p", "b"))
        self.assertFalse(await limiter.allow("p", "c"))
        now[0] = 5
        self.assertTrue(await limiter.allow("p", "c"))
        self.assertNotIn(("p", "a"), limiter.group_events)

    async def test_invalid_limits(self):
        with self.assertRaises(ValueError):
            RateLimiter(global_limit=True, group_limit=1, window_seconds=1)
        with self.assertRaises(ValueError):
            RateLimiter(global_limit=1, group_limit=1, window_seconds=float("nan"))
