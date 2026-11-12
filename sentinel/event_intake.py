"""Bounded nonblocking ingress between AstrBot events and the dry-run queue."""

import asyncio
from collections import Counter
from dataclasses import dataclass

from .dry_run_queue import SYSTEM_PLATFORM, OVERFLOW_GROUP
from .async_io import run_sync_safely


@dataclass(frozen=True)
class Skip:
    platform_id: str
    group_id: str
    message_id: str
    reason: str
    session_origin: str | None = None


class EventIntake:
    """Copy-only inbox; normal event handlers never wait for SQLite or models.

    This temporary in-memory buffer is not the durable queue. If it fills,
    dropped events are counted and periodically recorded as aggregated skips.
    A process crash can lose buffered messages and unflushed overflow counts.
    """

    def __init__(self, runtime, queue, *, capacity: int, on_failure=None):
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("invalid ingress capacity")
        self.runtime = runtime
        self.queue = queue
        self.inbox = asyncio.Queue(maxsize=capacity)
        self.overflows = {}
        self.failures = 0
        self.counts = Counter()
        self.closed = False
        self.task = None
        self._close_task = None
        self.on_failure = on_failure

    def _report_failure(self):
        self.failures += 1
        if self.on_failure is not None:
            try:
                self.on_failure("ingress_failure")
            except Exception:
                pass

    def start(self):
        if self.task is not None or self.closed:
            raise RuntimeError("ingress already started or closed")
        self.task = asyncio.create_task(self._run())
        self.task.add_done_callback(self._consumer_done)

    def _consumer_done(self, task):
        if task.cancelled() or task.exception() is not None or not self.closed:
            self._report_failure()

    def offer(self, item):
        if self.closed or self.task is None or self.task.done():
            return "ingress_stopped"
        try:
            self.inbox.put_nowait(item)
            return "accepted"
        except asyncio.QueueFull:
            # Session authorization is asynchronous and has not run yet.
            # Keep only a global count, not identities from denied sessions.
            key = (SYSTEM_PLATFORM, OVERFLOW_GROUP)
            self.overflows[key] = self.overflows.get(key, 0) + 1
            self.counts["ingress_full"] += 1
            return "ingress_full"

    async def _flush_overflows(self):
        pending, self.overflows = self.overflows, {}
        for (platform_id, group_id), count in pending.items():
            try:
                await run_sync_safely(self.queue.record_skip, platform_id=platform_id,
                    group_id=group_id, message_id="ingress-overflow", reason=f"ingress_full_count={count}")
            except Exception:
                self._report_failure()

    async def _run(self):
        while True:
            item = await self.inbox.get()
            try:
                if item is None:
                    await self._flush_overflows()
                    return
                if isinstance(item, Skip):
                    result = await self.runtime.record_external_skip(item)
                else:
                    result = await self.runtime.submit(item)
                known = {"queued", "duplicate", "queue_full", "skipped", "framework_session_denied",
                         "no_enabled_rule", "group_not_allowed", "group_blocked",
                         "invalid_list_mode", "settings_changed_reload_required", "runtime_stopped",
                         "invalid_message_identity", "empty_text", "message_too_long", "rate_limited",
                         "non_text_or_unknown", "management_command_or_unknown", "self_message",
                         "bot_identity_unverified"}
                self.counts[result if result in known else "other"] += 1
                if self.overflows:
                    await self._flush_overflows()
            except Exception:
                # No message contents or exception strings are retained here.
                self._report_failure()
            finally:
                self.inbox.task_done()

    async def close(self):
        if self._close_task is None:
            self.closed = True
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        if self.task is None:
            return
        if not self.task.done():
            sentinel = asyncio.create_task(self.inbox.put(None))
            done, _ = await asyncio.wait((self.task, sentinel), return_when=asyncio.FIRST_COMPLETED)
            if sentinel not in done:
                sentinel.cancel()
                await asyncio.gather(sentinel, return_exceptions=True)
        await asyncio.gather(self.task, return_exceptions=True)
        # The consumer can stop unexpectedly with snapshots still in memory.
        # Discard those references; they were never accepted into durable work.
        while not self.inbox.empty():
            self.inbox.get_nowait()
            self.inbox.task_done()
        if self.overflows:
            await self._flush_overflows()
