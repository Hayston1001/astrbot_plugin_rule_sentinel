import asyncio
import unittest
import tempfile
import threading
from pathlib import Path
from contextlib import closing

from sentinel.notifications import NotificationDispatcher, NotificationService, build_notification_dispatcher
from sentinel.dry_run_queue import DryRunQueue


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_failures_are_rate_limited_and_audited(self):
        sent, audit = [], []
        async def send(recipient, text):
            sent.append((recipient, text))
            raise RuntimeError("private error")
        async def record(code, status):
            audit.append((code, status))
        service = NotificationService(recipient="platform:FriendMessage:user", interval_seconds=30,
                                      send=send, audit=record, clock=lambda: 10)
        result = await asyncio.gather(service.notify("model_failed"), service.notify("model_failed"))
        self.assertCountEqual(result, ["unknown", "rate_limited"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(audit, [("model_failed", "unknown")])
        self.assertNotIn("private error", sent[0][1])

    async def test_invalid_configuration_and_unknown_code(self):
        with self.assertRaises(ValueError):
            NotificationService(recipient="", interval_seconds=1, send=None, audit=None)
        with self.assertRaises(ValueError):
            NotificationService(recipient="platform:GroupMessage:group", interval_seconds=1, send=None, audit=None)
        service = NotificationService(recipient="platform:FriendMessage:user", interval_seconds=1, send=None, audit=None)
        with self.assertRaises(ValueError):
            await service.notify("unknown")

    async def test_platform_found_does_not_claim_delivery(self):
        results = []
        async def send(recipient, text):
            return True
        async def audit(code, status):
            results.append(status)
        service = NotificationService(recipient="p:FriendMessage:u", interval_seconds=1, send=send, audit=audit)
        self.assertEqual(await service.notify("model_failed"), "submitted")
        self.assertEqual(results, ["submitted"])

    async def test_dispatcher_records_result_without_sending_to_group(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = DryRunQueue(Path(temp) / "state.sqlite")
            queue.initialize()
            class Context:
                async def send_message(self, target, message):
                    self.target = target
                    return False
            context = Context()
            dispatcher = build_notification_dispatcher(context=context, queue=queue,
                recipient="p:FriendMessage:admin", interval_seconds=60, message_chain_factory=lambda text: text)
            dispatcher.start()
            self.assertTrue(dispatcher.offer("model_failed"))
            await dispatcher.close()
            self.assertEqual(context.target, "p:FriendMessage:admin")
            with closing(queue._connect()) as db:
                result = db.execute("SELECT code, status FROM notification_events").fetchone()
                self.assertEqual(tuple(result), ("model_failed", "failed"))

    async def test_revoked_notification_authorization_does_not_send(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = DryRunQueue(Path(temp) / "state.sqlite")
            queue.initialize()
            class Context:
                async def send_message(self, target, message):
                    raise AssertionError("revoked notification was sent")
            dispatcher = build_notification_dispatcher(context=Context(), queue=queue,
                recipient="p:FriendMessage:admin", interval_seconds=60,
                message_chain_factory=lambda text: text, is_authorized=lambda: False)
            dispatcher.start()
            dispatcher.offer("model_failed")
            await dispatcher.close()
            with closing(queue._connect()) as db:
                self.assertEqual(db.execute("SELECT status FROM notification_events").fetchone()[0], "failed")

    async def test_failed_consumer_rejects_new_items_and_closes_with_full_queue(self):
        entered = asyncio.Event()
        class Service:
            async def notify(self, code):
                entered.set()
                await asyncio.Event().wait()
        failures = []
        dispatcher = NotificationDispatcher(Service(), capacity=1, on_failure=failures.append)
        dispatcher.start()
        self.assertTrue(dispatcher.offer("model_failed"))
        await asyncio.wait_for(entered.wait(), 2)
        self.assertTrue(dispatcher.offer("queue_full"))
        closing_task = asyncio.create_task(dispatcher.close())
        await asyncio.sleep(0)
        dispatcher.task.cancel()
        await asyncio.wait_for(closing_task, 2)
        await asyncio.sleep(0)
        self.assertEqual(failures, ["notification_consumer_failed"])
        self.assertEqual(dispatcher.dropped, 1)
        self.assertFalse(dispatcher.offer("model_failed"))
        await asyncio.wait_for(dispatcher.queue.join(), 2)
        await dispatcher.close()

    async def test_cancelled_consumer_waits_for_notification_audit_thread(self):
        entered, release = threading.Event(), threading.Event()
        class Queue:
            def record_notification(self, code, status):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("audit test timed out")
        class Context:
            async def send_message(self, target, message):
                return True
        dispatcher = build_notification_dispatcher(context=Context(), queue=Queue(),
            recipient="p:FriendMessage:admin", interval_seconds=60, message_chain_factory=lambda text: text)
        dispatcher.start()
        dispatcher.offer("model_failed")
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            dispatcher.task.cancel()
            close = asyncio.create_task(dispatcher.close())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(close.done())
        finally:
            release.set()
        await asyncio.wait_for(close, 3)
        self.assertEqual(dispatcher.failures, 1)

    async def test_dead_consumer_rejects_offers_before_shutdown(self):
        class Service:
            async def notify(self, code):
                raise asyncio.CancelledError()
        dispatcher = NotificationDispatcher(Service(), capacity=2)
        dispatcher.start()
        self.assertTrue(dispatcher.offer("model_failed"))
        await asyncio.gather(dispatcher.task, return_exceptions=True)
        self.assertFalse(dispatcher.offer("queue_full"))
        self.assertEqual(dispatcher.dropped, 1)
        await dispatcher.close()

    async def test_cancelled_close_does_not_abandon_sender(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class Service:
            async def notify(self, code):
                entered.set()
                await release.wait()
        dispatcher = NotificationDispatcher(Service(), capacity=1)
        dispatcher.start()
        self.assertTrue(dispatcher.offer("model_failed"))
        await asyncio.wait_for(entered.wait(), 2)
        closing_task = asyncio.create_task(dispatcher.close())
        await asyncio.sleep(0)
        closing_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing_task
        self.assertFalse(dispatcher.task.done())
        release.set()
        await asyncio.wait_for(dispatcher.close(), 2)
        self.assertTrue(dispatcher.task.done())
