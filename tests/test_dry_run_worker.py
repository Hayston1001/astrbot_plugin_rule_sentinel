import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sentinel.dry_run_queue import DryRunQueue
from sentinel.dry_run_worker import Decision, DryRunWorker
from sentinel.circuit_breaker import CircuitBreaker
from sentinel.decision_errors import ProviderIntegrityUnverified


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "queue.sqlite"
        self.queue = DryRunQueue(self.path)
        self.queue.initialize()

    def enqueue(self, group, message):
        self.queue.enqueue(platform_id="p", group_id=group, message_id=message,
                           payload={"text": "private"}, capacity=5)

    def events(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT message_id, status, detail FROM dry_run_events ORDER BY id").fetchall()

    async def test_group_order_and_failure_are_recorded(self):
        self.enqueue("g", "1")
        self.enqueue("g", "2")
        async def decide(item):
            if item.message_id == "1":
                return Decision("review", "needs_review")
            raise RuntimeError("secret private message")
        worker = DryRunWorker(self.queue, decide, concurrency=1, timeout_seconds=1)
        self.assertEqual(await worker.drain_group("p", "g"), 2)
        self.assertEqual(self.events(), [("1", "review", "needs_review"), ("2", "failed", "decision_error")])

    async def test_timeout_is_not_no_action(self):
        self.enqueue("g", "1")
        async def decide(item):
            await asyncio.sleep(1)
        worker = DryRunWorker(self.queue, decide, concurrency=1, timeout_seconds=0.01)
        self.assertEqual(await worker.drain_group("p", "g"), 1)
        self.assertEqual(self.events(), [("1", "timed_out", "decision_timeout")])

    async def test_failed_claim_notifies_without_attempting_model(self):
        self.enqueue("g", "1")
        def fail(*args):
            raise sqlite3.OperationalError("database unavailable")
        self.queue.claim = fail
        notifications = []
        async def decide(item):
            self.fail("failed claim reached model")
        worker = DryRunWorker(self.queue, decide, concurrency=1, timeout_seconds=1, notify=notifications.append)
        with self.assertRaises(sqlite3.OperationalError):
            await worker.drain_group("p", "g")
        self.assertEqual(notifications, ["storage_failed"])
        self.assertEqual(self.events(), [])

    async def test_corrupt_recovered_work_is_failed_and_next_message_runs(self):
        self.enqueue("g", "1")
        self.enqueue("g", "2")
        stale = self.queue.claim("p", "g")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE dry_run_queue SET payload=? WHERE message_id='1'", ('{"text":"private',))
            db.commit()
        self.assertEqual(self.queue.recover_after_exclusive_start(), 1)
        calls = []
        async def decide(item):
            calls.append(item.message_id)
            return Decision("no_action", "no_tool_calls")
        worker = DryRunWorker(self.queue, decide, concurrency=1, timeout_seconds=1)
        self.assertEqual(await worker.drain_group("p", "g"), 1)
        self.assertEqual(calls, ["2"])
        self.assertEqual(self.events(), [("1", "failed", "invalid_queue_payload"), ("2", "no_action", "no_tool_calls")])
        self.assertFalse(self.queue.finish(stale, status="no_action", detail="stale"))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT payload FROM dry_run_queue ORDER BY id").fetchall(), [(None,), (None,)])

    async def test_global_concurrency_is_bounded(self):
        for group in ("a", "b", "c"):
            self.enqueue(group, group)
        active = 0
        maximum = 0
        async def decide(item):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return Decision("no_action", "no_tool_calls")
        worker = DryRunWorker(self.queue, decide, concurrency=2, timeout_seconds=1)
        self.assertEqual(await asyncio.gather(*(worker.drain_group("p", group) for group in ("a", "b", "c"))), [1, 1, 1])
        self.assertLessEqual(maximum, 2)

    async def test_cancellation_requires_recovery(self):
        self.enqueue("g", "1")
        started = asyncio.Event()
        async def decide(item):
            started.set()
            await asyncio.Event().wait()
        worker = DryRunWorker(self.queue, decide, concurrency=1, timeout_seconds=10)
        task = asyncio.create_task(worker.drain_group("p", "g"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.events(), [])
        self.assertEqual(self.queue.recover_after_exclusive_start(), 1)
        self.assertIsNotNone(self.queue.claim("p", "g"))

    async def test_circuit_skips_instead_of_marking_normal(self):
        self.enqueue("g", "1")
        self.enqueue("g", "2")
        async def decide(item):
            raise RuntimeError("service down")
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
        worker = DryRunWorker(self.queue, decide, concurrency=1, timeout_seconds=1, breaker=breaker)
        self.assertEqual(await worker.drain_group("p", "g"), 2)
        self.assertEqual(self.events(), [("1", "failed", "decision_error"), ("2", "skipped", "circuit_open")])

    async def test_cancelled_probe_settles_breaker_before_worker_exits(self):
        self.enqueue("g", "1")
        now = [0.0]
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=5, clock=lambda: now[0])
        await breaker.complete(await breaker.acquire(), success=False)
        now[0] = 5
        started, release = asyncio.Event(), asyncio.Event()
        async def decide(item):
            started.set()
            await release.wait()
            return Decision("no_action", "explicit_no_action")
        worker = DryRunWorker(self.queue, decide, concurrency=1, timeout_seconds=10, breaker=breaker)
        task = asyncio.create_task(worker.drain_group("p", "g"))
        await asyncio.wait_for(started.wait(), 2)
        await breaker.lock.acquire()
        try:
            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            breaker.lock.release()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertFalse(breaker.probing)
        self.assertIsNotNone(await breaker.acquire())
        self.assertEqual(self.events(), [])
        self.assertEqual(self.queue.recover_after_exclusive_start(), 1)

    async def test_oversized_audit_is_failure_without_stranding_queue(self):
        self.enqueue("g", "1")
        async def decide(item):
            return Decision("proposed", "x" * 100001)
        worker = DryRunWorker(self.queue, decide, concurrency=1, timeout_seconds=1)
        self.assertEqual(await worker.drain_group("p", "g"), 1)
        self.assertEqual(self.events(), [("1", "failed", "audit_detail_invalid_or_too_large")])

    async def test_stop_finishes_current_and_preserves_pending(self):
        self.enqueue("g", "1")
        self.enqueue("g", "2")
        started, release = asyncio.Event(), asyncio.Event()
        async def decide(item):
            started.set()
            await release.wait()
            return Decision("no_action", "explicit_no_action")
        worker = DryRunWorker(self.queue, decide, concurrency=1, timeout_seconds=1)
        task = asyncio.create_task(worker.drain_group("p", "g"))
        await started.wait()
        worker.request_stop()
        release.set()
        self.assertEqual(await task, 1)
        self.assertEqual(self.queue.claim("p", "g").message_id, "2")

    async def test_unverified_provider_is_skipped_not_counted_as_model_failure(self):
        self.enqueue("g", "1")
        async def decide(item):
            raise ProviderIntegrityUnverified("not verified")
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
        worker = DryRunWorker(self.queue, decide, concurrency=1, timeout_seconds=1, breaker=breaker)
        await worker.drain_group("p", "g")
        self.assertEqual(self.events(), [("1", "skipped", "provider_integrity_unverified")])
        self.assertIsNone(breaker.opened_at)
