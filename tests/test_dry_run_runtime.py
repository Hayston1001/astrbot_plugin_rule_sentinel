import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from sentinel.dry_run_ingest import DryRunAdmission
from sentinel.dry_run_queue import DryRunQueue
from sentinel.dry_run_runtime import DryRunRuntime
from sentinel.dry_run_worker import Decision, DryRunWorker
from sentinel.event_intake import EventIntake, Skip
from sentinel.monitoring_policy import MonitoringPolicy


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_arrival_during_drain_schedules_another_pass(self):
        started = asyncio.Event()
        finish_first = asyncio.Event()
        class Admission:
            async def submit(self, message):
                return "queued"
        class Worker:
            queue = SimpleNamespace(recover_after_exclusive_start=lambda: 0, pending_groups=lambda: [])
            count = 0
            async def drain_group(self, platform, group):
                self.count += 1
                if self.count == 1:
                    started.set()
                    await finish_first.wait()
        worker = Worker()
        runtime = DryRunRuntime(Admission(), worker)
        await runtime.start_after_exclusive_stop()
        message = SimpleNamespace(platform_id="p", group_id="g")
        await runtime.submit(message)
        await started.wait()
        await runtime.submit(message)
        finish_first.set()
        await runtime.close()
        self.assertEqual(worker.count, 2)
        self.assertEqual(await runtime.submit(message), "runtime_stopped")

    async def test_worker_errors_are_recorded_without_error_text(self):
        class Admission:
            async def submit(self, message):
                return "queued"
        class Worker:
            queue = SimpleNamespace(recover_after_exclusive_start=lambda: 0, pending_groups=lambda: [])
            async def drain_group(self, platform, group):
                raise RuntimeError("private content")
        runtime = DryRunRuntime(Admission(), Worker())
        await runtime.start_after_exclusive_stop()
        await runtime.submit(SimpleNamespace(platform_id="p", group_id="g"))
        await runtime.close()
        self.assertEqual(list(runtime.failures), [(("p", "g"), "worker_failed")])

    async def test_cancelled_worker_is_reported_and_close_waits_for_other_group(self):
        started = {key: asyncio.Event() for key in ("a", "b")}
        release = asyncio.Event()
        class Admission:
            async def submit(self, message):
                return "queued"
        class Worker:
            queue = SimpleNamespace(recover_after_exclusive_start=lambda: 0, pending_groups=lambda: [])
            async def drain_group(self, platform, group):
                started[group].set()
                await release.wait()
        reported = []
        runtime = DryRunRuntime(Admission(), Worker(), on_failure=reported.append)
        await runtime.start_after_exclusive_stop()
        for group in ("a", "b"):
            await runtime.submit(SimpleNamespace(platform_id="p", group_id=group))
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 2)
        first = runtime._tasks[("p", "a")]
        close = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        self.assertFalse(close.done())
        self.assertEqual(reported, ["worker_cancelled"])
        release.set()
        await asyncio.wait_for(close, 2)
        self.assertEqual(list(runtime.failures), [(("p", "a"), "worker_cancelled")])

    async def test_cancelled_worker_lease_is_retained_until_exclusive_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = DryRunQueue(Path(temp) / "state.sqlite")
            queue.initialize()
            for group in ("a", "b"):
                queue.enqueue(platform_id="p", group_id=group, message_id="m", capacity=1,
                              payload={"text": "synthetic"})
            started = {group: asyncio.Event() for group in ("a", "b")}
            release = asyncio.Event()
            async def decide(item):
                started[item.group_id].set()
                await release.wait()
                return Decision("no_action", "synthetic")
            runtime = DryRunRuntime(None, DryRunWorker(queue, decide, concurrency=2, timeout_seconds=10))
            await runtime.start_after_exclusive_stop()
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 2)
            cancelled = runtime._tasks[("p", "a")]
            close = asyncio.create_task(runtime.close())
            await asyncio.sleep(0)
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            self.assertFalse(close.done())
            self.assertIsNone(queue.claim("p", "a"))
            release.set()
            await asyncio.wait_for(close, 2)
            self.assertEqual(list(runtime.failures), [(("p", "a"), "worker_cancelled")])
            self.assertEqual(queue.list_events(platform_id="p", group_id="a"), [])
            self.assertEqual(queue.list_events(platform_id="p", group_id="b")[0]["status"], "no_action")
            self.assertEqual(queue.recover_after_exclusive_start(), 1)
            self.assertEqual(queue.claim("p", "a").payload["text"], "synthetic")

    async def test_close_waits_for_admission_even_if_caller_is_cancelled(self):
        started, release = asyncio.Event(), asyncio.Event()
        class Admission:
            async def submit(self, message):
                started.set()
                await release.wait()
                return "queued"
        class Worker:
            queue = SimpleNamespace(recover_after_exclusive_start=lambda: 0, pending_groups=lambda: [])
            async def drain_group(self, platform, group):
                return 0
        runtime = DryRunRuntime(Admission(), Worker())
        await runtime.start_after_exclusive_stop()
        submit = asyncio.create_task(runtime.submit(SimpleNamespace(platform_id="p", group_id="g")))
        await started.wait()
        submit.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await submit
        close = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        self.assertFalse(close.done())
        release.set()
        await close

    async def test_close_waits_for_startup_recovery_thread(self):
        entered, release = threading.Event(), threading.Event()
        class Queue:
            def recover_after_exclusive_start(self):
                entered.set()
                release.wait(timeout=2)
            def pending_groups(self):
                return []
        class Worker:
            queue = Queue()
        runtime = DryRunRuntime(None, Worker())
        start = asyncio.create_task(runtime.start_after_exclusive_stop())
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        close = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        self.assertFalse(close.done())
        release.set()
        with self.assertRaises(RuntimeError):
            await start
        await close

    async def test_cancelled_skip_consumer_does_not_abandon_audit_write(self):
        entered, release = threading.Event(), threading.Event()
        records = []
        class Queue:
            def recover_after_exclusive_start(self):
                return 0
            def pending_groups(self):
                return []
            def record_skip(self, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("audit test timed out")
                records.append(kwargs)
        class Rules:
            def current_for_group(self, platform_id, group_id):
                return [SimpleNamespace(enabled=True)]
        class Worker:
            queue = Queue()
        async def gate(message):
            return True
        queue = Worker.queue
        admission = DryRunAdmission(queue, Rules(), policy=MonitoringPolicy("allowlist", frozenset({("p", "g")})),
                                    capacity=1, model="synthetic", session_gate=gate)
        runtime = DryRunRuntime(admission, Worker())
        await runtime.start_after_exclusive_stop()
        intake = EventIntake(runtime, queue, capacity=1)
        intake.start()
        intake.offer(Skip("p", "g", "m", "bot_identity_unverified", "p:GroupMessage:g"))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            intake.task.cancel()
            intake_close = asyncio.create_task(intake.close())
            await asyncio.wait_for(intake_close, 2)
            runtime_close = asyncio.create_task(runtime.close())
            await asyncio.sleep(0)
            self.assertFalse(runtime_close.done())
        finally:
            release.set()
        await asyncio.wait_for(runtime_close, 2)
        self.assertEqual(records[0]["reason"], "bot_identity_unverified")
        self.assertEqual(intake.failures, 1)
