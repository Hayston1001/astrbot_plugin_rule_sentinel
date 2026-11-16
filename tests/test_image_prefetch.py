import asyncio
import unittest

from sentinel.image_fetch import FetchedImage, ImageFetchError
from sentinel.image_prefetch import ImagePrefetcher

IMAGE = FetchedImage("data:image/png;base64," + "A" * 16, 4)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ImagePrefetchTests(unittest.IsolatedAsyncioTestCase):
    async def drain(self, prefetcher):
        await asyncio.gather(*list(prefetcher._tasks), return_exceptions=True)

    async def test_scheduled_result_is_consumed_once(self):
        calls = []

        async def fetch(urls):
            calls.append(urls)
            return (IMAGE,)

        prefetcher = ImagePrefetcher(fetch, clock=Clock())
        try:
            self.assertTrue(prefetcher.schedule(("p", "g", "m"), ["https://example.com/a"]))
            self.assertFalse(prefetcher.schedule(("p", "g", "m"), ["https://example.com/a"]))
            await self.drain(prefetcher)
            cached = prefetcher.take(("p", "g", "m"))
            self.assertIsNotNone(cached)
            self.assertEqual(cached.images, (IMAGE,))
            self.assertIsNone(cached.code)
            self.assertIsNone(prefetcher.take(("p", "g", "m")))
            self.assertEqual(calls, [("https://example.com/a",)])
        finally:
            await prefetcher.close()

    async def test_failure_code_is_cached_then_consumed(self):
        async def fetch(urls):
            raise ImageFetchError("image_fetch_failed")

        prefetcher = ImagePrefetcher(fetch, clock=Clock())
        try:
            prefetcher.schedule(("p", "g", "m"), ["https://example.com/a"])
            await self.drain(prefetcher)
            cached = prefetcher.take(("p", "g", "m"))
            self.assertEqual((cached.images, cached.code), (None, "image_fetch_failed"))
        finally:
            await prefetcher.close()

    async def test_unexpected_errors_become_a_fetch_failure(self):
        async def fetch(urls):
            raise RuntimeError("private text")

        prefetcher = ImagePrefetcher(fetch, clock=Clock())
        try:
            prefetcher.schedule(("p", "g", "m"), ["https://example.com/a"])
            await self.drain(prefetcher)
            self.assertEqual(prefetcher.take(("p", "g", "m")).code, "image_fetch_failed")
        finally:
            await prefetcher.close()

    async def test_expired_entries_are_not_reused(self):
        clock = Clock()

        async def fetch(urls):
            return (IMAGE,)

        prefetcher = ImagePrefetcher(fetch, ttl_seconds=5, clock=clock)
        try:
            prefetcher.schedule(("p", "g", "m"), ["https://example.com/a"])
            await self.drain(prefetcher)
            clock.now = 6
            self.assertIsNone(prefetcher.take(("p", "g", "m")))
        finally:
            await prefetcher.close()

    async def test_budget_evicts_oldest_entry(self):
        clock = Clock()

        async def fetch(urls):
            return (FetchedImage("data:image/png;base64," + "A" * 40, 30),)

        prefetcher = ImagePrefetcher(fetch, max_cache_bytes=120, clock=clock)
        try:
            for index in range(3):
                clock.now = index
                prefetcher.schedule(("p", "g", str(index)), ["https://example.com/a"])
                await self.drain(prefetcher)
            self.assertIsNone(prefetcher.take(("p", "g", "0")))
            self.assertIsNotNone(prefetcher.take(("p", "g", "2")))
        finally:
            await prefetcher.close()

    async def test_consume_waits_for_an_inflight_prefetch(self):
        release = asyncio.Event()

        async def fetch(urls):
            await release.wait()
            return (IMAGE,)

        prefetcher = ImagePrefetcher(fetch, wait_seconds=1, clock=Clock())
        try:
            self.assertTrue(prefetcher.schedule(("p", "g", "m"), ["https://example.com/a"]))
            consumer = asyncio.create_task(prefetcher.consume(("p", "g", "m")))
            await asyncio.sleep(0)
            release.set()
            cached = await asyncio.wait_for(consumer, 1)
            self.assertEqual(cached.images, (IMAGE,))
            self.assertIsNone(prefetcher.take(("p", "g", "m")))
        finally:
            await prefetcher.close()

    async def test_close_cancels_pending_work(self):
        started = asyncio.Event()

        async def fetch(urls):
            started.set()
            await asyncio.sleep(5)
            return (IMAGE,)

        prefetcher = ImagePrefetcher(fetch, clock=Clock())
        prefetcher.schedule(("p", "g", "m"), ["https://example.com/a"])
        await started.wait()
        await prefetcher.close()
        self.assertTrue(prefetcher.closed)
        self.assertIsNone(prefetcher.take(("p", "g", "m")))
        self.assertFalse(prefetcher.schedule(("p", "g", "other"), ["https://example.com/b"]))


if __name__ == "__main__":
    unittest.main()
