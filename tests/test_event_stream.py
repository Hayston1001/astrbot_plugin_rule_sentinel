import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from sentinel.dry_run_queue import DryRunQueue
from sentinel.event_stream import EventHub


class EventStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = DryRunQueue(Path(self.tmp.name) / "queue.sqlite3")
        self.queue.initialize()
        self.hub = EventHub(asyncio.get_running_loop(), limit=2, capacity=2)
        self.queue.event_sink = self.hub.publish

    async def test_start_finish_snapshot_and_group_isolation(self):
        sub = self.hub.subscribe("p", "g")
        other = self.hub.subscribe("p", "other")
        with self.assertRaises(RuntimeError):
            self.hub.subscribe("p", "g")
        stream = sub.stream(self.queue)
        self.assertIn("event: ping", await anext(stream))
        await asyncio.to_thread(self.queue.enqueue, platform_id="p", group_id="g", message_id="m", payload={}, capacity=5)
        item = await asyncio.to_thread(self.queue.claim, "p", "g")
        await asyncio.to_thread(self.queue.record_evaluation_start, item, "{}")
        await asyncio.to_thread(self.queue.finish, item, status="failed", detail="bad")
        first = json.loads((await anext(stream)).removeprefix("data: "))
        second = json.loads((await anext(stream)).removeprefix("data: "))
        self.assertEqual((first["status"], second["status"]), ("started", "failed"))
        self.assertEqual(len(second["events"]), 2)
        self.assertTrue(other.queue.empty())
        await stream.aclose()
        self.assertNotIn(sub, self.hub.subscribers)
        self.hub.close()
        self.assertEqual(len(self.hub.subscribers), 0)

    async def test_overflow_reports_resync(self):
        sub = self.hub.subscribe("p", "g")
        stream = sub.stream(self.queue)
        await anext(stream)
        for i in range(3):
            await asyncio.to_thread(self.queue.record_skip, platform_id="p", group_id="g", message_id=str(i), reason="skip")
        self.assertIn("event: resync", await anext(stream))
        await stream.aclose()

    async def test_all_groups_delivers_scoped_records_but_not_system_events(self):
        sub = self.hub.subscribe(None, None)
        stream = sub.stream(self.queue)
        await anext(stream)
        for platform, group in (("p1", "g"), ("p2", "g"), ("__rule_sentinel_system__", "__ingress_overflow__")):
            await asyncio.to_thread(self.queue.record_skip, platform_id=platform, group_id=group, message_id="same", reason="skip")
        first = json.loads((await anext(stream)).removeprefix("data: "))
        second = json.loads((await anext(stream)).removeprefix("data: "))
        self.assertEqual([(row["platform_id"], row["group_id"], row["message_id"]) for row in (first, second)],
                         [("p1", "g", "same"), ("p2", "g", "same")])
        self.assertTrue(sub.queue.empty())
        await stream.aclose()

    async def test_cancellation_and_full_queue_skip(self):
        sub = self.hub.subscribe("p", "g")
        stream = sub.stream(self.queue)
        await anext(stream)
        await asyncio.to_thread(self.queue.enqueue, platform_id="p", group_id="g", message_id="m1", payload={}, capacity=1)
        await asyncio.to_thread(self.queue.enqueue, platform_id="p", group_id="g", message_id="m2", payload={}, capacity=1)
        row = json.loads((await anext(stream)).removeprefix("data: "))
        self.assertEqual((row["status"], row["detail"]), ("skipped", "queue_full"))
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(sub, self.hub.subscribers)
