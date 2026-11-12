"""Lifecycle coordinator for the inactive dry-run implementation."""

import asyncio
from collections import deque

from .async_io import run_sync_safely


class DryRunRuntime:
    """Schedule group drains without losing arrivals during an active drain.

    Startup recovery is only safe with exclusive ownership, after all old
    workers and their database threads have stopped. The host must enforce it.
    No platform event or management handler is registered here.
    """

    def __init__(self, admission, worker, *, on_failure=None):
        self.admission = admission
        self.worker = worker
        self.queue = worker.queue
        self._tasks = {}
        self._generation = {}
        self._closed = False
        self._started = False
        self._starting = False
        self._admissions = set()
        self.failures = deque(maxlen=100)
        self.on_failure = on_failure
        self._startup_done = asyncio.Event()
        self._startup_done.set()

    async def start_after_exclusive_stop(self):
        if self._closed or self._started or self._starting or self._tasks:
            raise RuntimeError("runtime cannot be started again")
        self._starting = True
        self._startup_done.clear()
        try:
            await run_sync_safely(self.queue.recover_after_exclusive_start)
            groups = await run_sync_safely(self.queue.pending_groups)
            if self._closed:
                raise RuntimeError("runtime closed during startup")
            self._started = True
            for platform_id, group_id in groups:
                self._wake(platform_id, group_id)
        finally:
            self._starting = False
            self._startup_done.set()

    async def submit(self, message):
        if self._closed:
            return "runtime_stopped"
        if not self._started:
            return "runtime_not_started"
        async def admit():
            result = await self.admission.submit(message)
            if result in ("queued", "duplicate") and not self._closed:
                self._wake(message.platform_id, message.group_id)
            return result
        return await self._track_admission(admit(), (message.platform_id, message.group_id))

    async def _track_admission(self, operation, key):
        task = asyncio.create_task(operation)
        self._admissions.add(task)
        def complete(done):
            self._admissions.discard(done)
            if done.cancelled():
                self._report_failure(key, "admission_cancelled")
            elif done.exception() is not None:
                self._report_failure(key, "admission_failed")
        task.add_done_callback(complete)
        # An event handler can be cancelled while a SQLite thread is running.
        # Let the owned admission finish before shutdown or recovery.
        return await asyncio.shield(task)

    async def record_external_skip(self, skipped):
        if self._closed or not self._started:
            return "runtime_stopped"
        return await self._track_admission(self.admission.record_external_skip(skipped),
                                           (skipped.platform_id, skipped.group_id))

    def _wake(self, platform_id, group_id):
        key = (platform_id, group_id)
        self._generation[key] = self._generation.get(key, 0) + 1
        if key not in self._tasks:
            self._tasks[key] = asyncio.create_task(self._drain(key))

    def _report_failure(self, key, reason):
        self.failures.append((key, reason))
        if self.on_failure is not None:
            try:
                self.on_failure(reason)
            except Exception:
                pass

    async def _drain(self, key):
        try:
            while True:
                generation = self._generation[key]
                await self.worker.drain_group(*key)
                if generation == self._generation[key]:
                    break
        except asyncio.CancelledError:
            # A cancelled worker can leave a lease for exclusive recovery.
            self._report_failure(key, "worker_cancelled")
            raise
        except Exception:
            # Do not store exception messages, which may contain private data.
            self._report_failure(key, "worker_failed")
        finally:
            self._tasks.pop(key, None)
            self._generation.pop(key, None)

    async def close(self):
        """Wait for active workers; do not cancel outstanding SQLite threads."""
        self._closed = True
        self.stop_new_decisions()
        await self._startup_done.wait()
        admissions = list(self._admissions)
        if admissions:
            await asyncio.gather(*admissions, return_exceptions=True)
        tasks = list(self._tasks.values())
        if tasks:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
                    raise outcome

    def stop_new_decisions(self):
        request_stop = getattr(self.worker, "request_stop", None)
        if callable(request_stop):
            request_stop()
