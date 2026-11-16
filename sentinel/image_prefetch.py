"""Bounded in-memory prefetch cache for chat images.

Prefetching starts after a message is durably queued, so a slow or expired
image link cannot hold up the event handler or the intake. The cache is only an
optimisation: a miss, an expiry, or eviction simply makes the judge fetch the
image inline with the same limits.

Nothing is written to disk. Entries are consumed once by the judge and evicted
oldest-first once the encoded budget is reached.
"""

import asyncio
import time
from dataclasses import dataclass

from .image_fetch import ImageFetchError


@dataclass(frozen=True)
class PrefetchedImages:
    images: tuple | None
    code: str | None
    encoded_bytes: int


@dataclass(frozen=True)
class _Entry:
    images: tuple | None
    code: str | None
    encoded_bytes: int
    created_at: float


class ImagePrefetcher:
    """Download images in the background and hand them to one consumer."""

    def __init__(self, fetch, *, ttl_seconds=600, max_cache_bytes=67108864,
                 concurrency=4, max_pending=256, wait_seconds=10, clock=None):
        if not callable(fetch):
            raise ValueError("image fetch callable is required")
        if any(type(value) is not int or value <= 0
               for value in (ttl_seconds, max_cache_bytes, concurrency, max_pending, wait_seconds)):
            raise ValueError("invalid prefetch limit")
        self.fetch = fetch
        self.ttl_seconds = ttl_seconds
        self.max_cache_bytes = max_cache_bytes
        self.max_pending = max_pending
        self.wait_seconds = wait_seconds
        self.clock = clock or time.monotonic
        self._limit = asyncio.Semaphore(concurrency)
        self._cache = {}
        self._pending = set()
        self._tasks_by_key = {}
        self._bytes = 0
        self._tasks = set()
        self.closed = False

    def _prune(self, now):
        expired = [key for key, entry in self._cache.items()
                   if now - entry.created_at > self.ttl_seconds]
        for key in expired:
            self._drop(key)

    def _drop(self, key):
        entry = self._cache.pop(key, None)
        if entry is not None:
            self._bytes = max(0, self._bytes - entry.encoded_bytes)

    def _store(self, key, *, images, code, encoded_bytes):
        now = self.clock()
        self._prune(now)
        if encoded_bytes > self.max_cache_bytes:
            return
        self._drop(key)
        while self._cache and self._bytes + encoded_bytes > self.max_cache_bytes:
            oldest = min(self._cache, key=lambda item: self._cache[item].created_at)
            self._drop(oldest)
        self._cache[key] = _Entry(images, code, encoded_bytes, now)
        self._bytes += encoded_bytes

    def schedule(self, key, urls):
        """Start a prefetch unless one is cached, pending, or the queue is full."""
        if self.closed or not urls or key in self._cache or key in self._pending:
            return False
        if len(self._pending) >= self.max_pending:
            return False
        self._pending.add(key)
        task = asyncio.create_task(self._load(key, tuple(urls)))
        self._tasks.add(task)
        self._tasks_by_key[key] = task
        def done(completed):
            self._tasks.discard(completed)
            self._pending.discard(key)
            if self._tasks_by_key.get(key) is completed:
                self._tasks_by_key.pop(key, None)
        task.add_done_callback(done)
        return True

    async def consume(self, key):
        """Return the cached result, waiting briefly for an in-flight prefetch."""
        entry = self.take(key)
        if entry is not None:
            return entry
        task = self._tasks_by_key.get(key)
        if task is None:
            return None
        try:
            await asyncio.wait_for(task, self.wait_seconds)
        except (TimeoutError, asyncio.TimeoutError):
            return None
        except Exception:
            pass
        return self.take(key)

    async def _load(self, key, urls):
        async with self._limit:
            try:
                images = await self.fetch(urls)
            except ImageFetchError as error:
                self._store(key, images=None, code=error.code, encoded_bytes=0)
                return
            except Exception:
                self._store(key, images=None, code="image_fetch_failed", encoded_bytes=0)
                return
            if self.closed:
                return
            encoded = sum(len(image.data_url) for image in images)
            self._store(key, images=images, code=None, encoded_bytes=encoded)

    def take(self, key):
        """Consume the cached result for one message, if it is still valid."""
        entry = self._cache.get(key)
        if entry is None:
            return None
        if self.clock() - entry.created_at > self.ttl_seconds:
            self._drop(key)
            return None
        self._drop(key)
        return PrefetchedImages(entry.images, entry.code, entry.encoded_bytes)

    async def close(self):
        self.closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._tasks_by_key.clear()
        self._pending.clear()
        self._cache.clear()
        self._bytes = 0
