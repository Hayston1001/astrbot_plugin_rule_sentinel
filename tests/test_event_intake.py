import asyncio
import threading
import unittest
from types import SimpleNamespace

from sentinel.event_intake import EventIntake, Skip
from sentinel.dry_run_queue import SYSTEM_PLATFORM, OVERFLOW_GROUP


class IntakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_nonblocking_and_counted_overflow(self):
        started, release = asyncio.Event(), asyncio.Event()
        class Runtime:
            async def submit(self, item):
                started.set()
                await release.wait()
        class Queue:
            def __init__(self):
                self.records = []
            def record_skip(self, **kwargs):
                self.records.append(kwargs)
        queue = Queue()
        intake = EventIntake(Runtime(), queue, capacity=1)
        intake.start()
        item = SimpleNamespace(platform_id="p", group_id="g")
        self.assertEqual(intake.offer(item), "accepted")
        await started.wait()
        self.assertEqual(intake.offer(item), "accepted")
        self.assertEqual(intake.offer(item), "ingress_full")
        self.assertEqual(intake.offer(item), "ingress_full")
        release.set()
        await intake.close()
        self.assertEqual(queue.records[0]["reason"], "ingress_full_count=2")
        self.assertEqual((queue.records[0]["platform_id"], queue.records[0]["group_id"]), (SYSTEM_PLATFORM, OVERFLOW_GROUP))
        self.assertEqual(intake.counts["ingress_full"], 2)
        self.assertEqual(intake.offer(item), "ingress_stopped")

    async def test_skipped_message_metadata_is_recorded(self):
        class Queue:
            def __init__(self):
                self.records = []
            def record_skip(self, **kwargs):
                self.records.append(kwargs)
        queue = Queue()
        class Runtime:
            async def record_external_skip(self, item):
                queue.record_skip(platform_id=item.platform_id, group_id=item.group_id,
                                  message_id=item.message_id, reason=item.reason)
        intake = EventIntake(Runtime(), queue, capacity=1)
        intake.start()
        self.assertEqual(intake.offer(Skip("p", "g", "id", "bot_identity_unverified")), "accepted")
        await intake.close()
        self.assertEqual(queue.records[0]["reason"], "bot_identity_unverified")

    async def test_ingress_failure_is_reported_without_private_error(self):
        class Runtime:
            async def record_external_skip(self, item):
                raise RuntimeError("private message")
        failures = []
        intake = EventIntake(Runtime(), None, capacity=1, on_failure=failures.append)
        intake.start()
        intake.offer(Skip("p", "g", "id", "bot_identity_unverified"))
        await intake.close()
        self.assertEqual(failures, ["ingress_failure"])
        self.assertEqual(intake.failures, 1)

    async def test_dead_consumer_rejects_offers_and_close_discards_buffer(self):
        started = asyncio.Event()
        class Runtime:
            async def submit(self, item):
                started.set()
                await asyncio.Event().wait()
        failures = []
        intake = EventIntake(Runtime(), None, capacity=1, on_failure=failures.append)
        intake.start()
        self.assertEqual(intake.offer("first"), "accepted")
        await started.wait()
        self.assertEqual(intake.offer("second"), "accepted")
        intake.task.cancel()
        await asyncio.gather(intake.task, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertEqual(intake.offer("third"), "ingress_stopped")
        await asyncio.wait_for(intake.close(), timeout=1)
        await asyncio.wait_for(intake.inbox.join(), timeout=1)
        self.assertEqual(intake.inbox.qsize(), 0)
        self.assertEqual(failures, ["ingress_failure"])

    async def test_concurrent_close_waits_for_same_drain(self):
        started, release = asyncio.Event(), asyncio.Event()
        class Runtime:
            async def submit(self, item):
                started.set()
                await release.wait()
                return "queued"
        intake = EventIntake(Runtime(), None, capacity=1)
        intake.start()
        intake.offer("first")
        await started.wait()
        first = asyncio.create_task(intake.close())
        second = asyncio.create_task(intake.close())
        await asyncio.sleep(0)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

    async def test_close_unblocks_if_full_consumer_fails_during_shutdown(self):
        started = asyncio.Event()
        class Runtime:
            async def submit(self, item):
                started.set()
                await asyncio.Event().wait()
        failures = []
        intake = EventIntake(Runtime(), None, capacity=1, on_failure=failures.append)
        intake.start()
        intake.offer("first")
        await started.wait()
        intake.offer("second")
        closing = asyncio.create_task(intake.close())
        await asyncio.sleep(0)
        intake.task.cancel()
        await asyncio.wait_for(closing, timeout=1)
        await asyncio.wait_for(intake.inbox.join(), timeout=1)
        self.assertEqual(failures, ["ingress_failure"])
        self.assertEqual(intake.inbox.qsize(), 0)

    async def test_cancelled_close_does_not_abandon_drain(self):
        started, release = asyncio.Event(), asyncio.Event()
        class Runtime:
            async def submit(self, item):
                started.set()
                await release.wait()
                return "queued"
        intake = EventIntake(Runtime(), None, capacity=1)
        intake.start()
        intake.offer("first")
        await started.wait()
        closing = asyncio.create_task(intake.close())
        await asyncio.sleep(0)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        release.set()
        await asyncio.wait_for(intake.close(), timeout=1)
        self.assertTrue(intake.task.done())

    async def test_cancelled_overflow_flush_waits_for_storage_thread(self):
        entered, release = threading.Event(), threading.Event()
        class Queue:
            def record_skip(self, **kwargs):
                entered.set()
                release.wait(timeout=5)
        intake = EventIntake(None, Queue(), capacity=1)
        intake.overflows[(SYSTEM_PLATFORM, OVERFLOW_GROUP)] = 1
        flush = asyncio.create_task(intake._flush_overflows())
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        flush.cancel()
        await asyncio.sleep(0)
        self.assertFalse(flush.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(flush, timeout=1)
