import asyncio
import unittest
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace

from sentinel.notifications import (MAX_NOTIFICATION_TEXT, NotificationDispatcher, NotificationScope,
                                   NotificationService, TEST_MESSAGE, build_notification_dispatcher,
                                   notification_scope, send_test_notifications)
from sentinel.dry_run_queue import DryRunQueue


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_failures_are_rate_limited_and_audited(self):
        sent, audit = [], []
        async def send(recipient, text):
            sent.append((recipient, text))
            raise RuntimeError("private error")
        async def record(code, status, summary):
            audit.append((code, status, summary))
        service = NotificationService(recipients=["platform:FriendMessage:user"], interval_seconds=30,
                                      send=send, audit=record, clock=lambda: 10)
        result = await asyncio.gather(service.notify("model_failed"), service.notify("model_failed"))
        self.assertCountEqual(result, ["unknown", "rate_limited"])
        self.assertEqual(len(sent), 1)
        self.assertEqual([item[:2] for item in audit], [("model_failed", "unknown")])
        self.assertIn("情况: 模型判定失败", sent[0][1])
        self.assertNotIn("private error", sent[0][1])
        self.assertEqual(audit[0][2], sent[0][1])

    async def test_notice_names_scope_impact_and_where_to_look(self):
        sent = []
        async def send(recipient, text):
            sent.append(text)
            return True
        async def record(code, status, summary):
            pass
        service = NotificationService(recipients=["p:FriendMessage:u"], interval_seconds=1, send=send,
                                      audit=record, wall_clock=lambda: datetime(2026, 11, 24, 14, 3))
        scope = NotificationScope(platform_id="qq_official", group_id="123456789", message_id="45678",
                                  group_label="产品群")
        self.assertEqual(await service.notify("model_failed", scope, "decision_error"), "submitted")
        lines = sent[0].splitlines()
        self.assertEqual(lines[0], "Rule Sentinel 失败提示")
        self.assertEqual(lines[1], "情况: 模型判定失败, 判定过程出错")
        self.assertEqual(lines[2], "范围: 群 产品群 (qq_official)")
        self.assertEqual(lines[3], "消息: 45678")
        self.assertEqual(lines[4], "时间: 11-24 14:03")
        self.assertIn("影响: 这条消息未完成判定", lines[5])
        self.assertTrue(lines[-1].startswith("去处: "))
        self.assertLessEqual(len(sent[0]), MAX_NOTIFICATION_TEXT)

    async def test_action_rejection_names_rule_action_and_duration(self):
        sent = []
        async def send(recipient, text):
            sent.append(text)
            return True
        async def record(code, status, summary):
            pass
        service = NotificationService(recipients=["p:FriendMessage:u"], interval_seconds=1, send=send,
                                      audit=record, wall_clock=lambda: datetime(2026, 11, 24, 14, 3))
        scope = NotificationScope(platform_id="qq", group_id="9", message_id="m", rule_id="r_1",
                                  action="mute", duration_seconds=600)
        await service.notify("action_rejected", scope)
        text = sent[0]
        self.assertIn("规则: r_1", text)
        self.assertIn("动作: 禁言 600 秒", text)
        self.assertIn("去处: 无需处理", text)

    async def test_merged_failures_count_groups_and_reset_after_sending(self):
        sent, now = [], [10.0]
        async def send(recipient, text):
            sent.append(text)
            return True
        async def record(code, status, summary):
            pass
        service = NotificationService(recipients=["p:FriendMessage:u"], interval_seconds=60, send=send,
                                      audit=record, clock=lambda: now[0], wall_clock=lambda: datetime(2026, 11, 24, 14, 3))
        await service.notify("queue_full", NotificationScope(platform_id="p", group_id="a"))
        self.assertEqual(await service.notify("queue_full", NotificationScope(platform_id="p", group_id="b")),
                         "rate_limited")
        await service.notify("queue_full", NotificationScope(platform_id="p", group_id="c"))
        now[0] = 100.0
        await service.notify("queue_full", NotificationScope(platform_id="p", group_id="c"))
        self.assertIn("合并: 自上次同类提示以来 3 次, 涉及 2 个群", sent[-1])
        now[0] = 200.0
        await service.notify("queue_full", NotificationScope(platform_id="p", group_id="b"))
        self.assertNotIn("合并:", sent[-1])

    async def test_group_label_lookup_is_optional_and_never_blocks_sending(self):
        sent = []
        async def send(recipient, text):
            sent.append(text)
            return True
        async def record(code, status, summary):
            pass
        async def resolve(scope):
            raise RuntimeError("lookup failed")
        service = NotificationService(recipients=["p:FriendMessage:u"], interval_seconds=1, send=send,
                                      audit=record, resolve_group_label=resolve, wall_clock=lambda: datetime(2026, 11, 24, 14, 3))
        self.assertEqual(await service.notify("model_failed", NotificationScope(platform_id="p", group_id="g")),
                         "submitted")
        self.assertIn("范围: 群 g (p)", sent[0])

    async def test_scope_helper_drops_unusable_values_instead_of_raising(self):
        scope = notification_scope(platform_id="p", group_id="g" * 500, message_id="m" * 500,
                                   rule_id="r", action="ban", duration_seconds="60",
                                   group_label="产品群")
        self.assertEqual((scope.platform_id, scope.group_id, scope.message_id, scope.rule_id,
                          scope.action, scope.duration_seconds, scope.group_label),
                         ("p", "", "", "r", "", None, "产品群"))

    async def test_scope_rejects_free_text_and_unexpected_values(self):
        for value in ({"group_id": "g"}, "g"):
            with self.assertRaises(ValueError):
                await NotificationService(recipients=["p:FriendMessage:u"], interval_seconds=1,
                                          send=None, audit=None).notify("model_failed", value)
        for field, value in (("group_id", "bad\nvalue"), ("rule_id", "x" * 129), ("action", "ban"),
                             ("duration_seconds", 0), ("duration_seconds", 2592001),
                             ("duration_seconds", "60"), ("group_label", "y" * 101)):
            with self.assertRaises(ValueError):
                NotificationScope(**{field: value})
        # An unknown failure detail is dropped instead of being echoed.
        sent = []
        async def send(recipient, text):
            sent.append(text)
            return True
        async def record(code, status, summary):
            pass
        service = NotificationService(recipients=["p:FriendMessage:u"], interval_seconds=1, send=send,
                                      audit=record, wall_clock=lambda: datetime(2026, 11, 24, 14, 3))
        await service.notify("model_failed", NotificationScope(platform_id="p"), "private group text")
        self.assertNotIn("private group text", sent[0])
        self.assertIn("范围: 实例 p", sent[0])

    async def test_invalid_configuration_and_unknown_code(self):
        with self.assertRaises(ValueError):
            NotificationService(recipients=[], interval_seconds=1, send=None, audit=None)
        with self.assertRaises(ValueError):
            NotificationService(recipients=["platform:GroupMessage:group"], interval_seconds=1, send=None, audit=None)
        service = NotificationService(recipients=["platform:FriendMessage:user"], interval_seconds=1, send=None, audit=None)
        with self.assertRaises(ValueError):
            await service.notify("unknown")

    async def test_platform_found_does_not_claim_delivery(self):
        results = []
        async def send(recipient, text):
            return True
        async def audit(code, status, summary):
            results.append(status)
        service = NotificationService(recipients=["p:FriendMessage:u"], interval_seconds=1, send=send, audit=audit)
        self.assertEqual(await service.notify("model_failed"), "submitted")
        self.assertEqual(results, ["submitted"])

    async def test_test_notification_sends_once_to_each_saved_recipient(self):
        calls = []

        async def send(target, chain):
            calls.append((target, chain))
            if target.endswith(":bad"):
                return False
            if target.endswith(":error"):
                raise RuntimeError("transport detail")
            return True

        context = SimpleNamespace(send_message=send)
        results = await send_test_notifications(
            context=context, targets=["p:FriendMessage:ok", "p:FriendMessage:bad", "p:FriendMessage:error"],
            message_chain_factory=lambda text: text)
        self.assertEqual(results, ["submitted", "failed", "unknown"])
        self.assertEqual([call[0] for call in calls],
                         ["p:FriendMessage:ok", "p:FriendMessage:bad", "p:FriendMessage:error"])
        self.assertTrue(all(call[1] == TEST_MESSAGE for call in calls))
        with self.assertRaises(ValueError):
            await send_test_notifications(context=context, targets=[], message_chain_factory=lambda text: text)
        with self.assertRaises(ValueError):
            await send_test_notifications(context=context, targets=["p:GroupMessage:g"],
                                          message_chain_factory=lambda text: text)

    async def test_fans_out_once_per_recipient_and_isolates_failures(self):
        targets = [f"bot{i}:FriendMessage:same-user" for i in range(5)]
        calls, results = [], []
        async def send(target, text):
            calls.append(target)
            if target == targets[0]:
                raise RuntimeError("private error")
            return target != targets[1]
        async def record(code, status, summary):
            results.append((code, status))
        service = NotificationService(recipients=targets, interval_seconds=60,
                                      send=send, audit=record, clock=lambda: 10)
        self.assertCountEqual(
            await asyncio.gather(service.notify("model_failed"), service.notify("model_failed")),
            ["rate_limited", "partial"],
        )
        self.assertCountEqual(calls, targets)
        self.assertCountEqual(results, [("model_failed", "unknown"), ("model_failed", "failed")] +
                              [("model_failed", "submitted")] * 3)

    async def test_dispatcher_records_result_without_sending_to_group(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = DryRunQueue(Path(temp) / "state.sqlite")
            queue.initialize()
            queue.save_group_name("p", "g", "产品群")
            class Context:
                async def send_message(self, target, message):
                    self.target = target
                    self.message = message
                    return False
            context = Context()
            dispatcher = build_notification_dispatcher(context=context, queue=queue,
                recipients=["p:FriendMessage:admin"], interval_seconds=60, message_chain_factory=lambda text: text)
            dispatcher.start()
            self.assertTrue(dispatcher.offer("model_failed",
                                             NotificationScope(platform_id="p", group_id="g", message_id="m"),
                                             "decision_error"))
            await dispatcher.close()
            self.assertEqual(context.target, "p:FriendMessage:admin")
            self.assertIn("情况: 模型判定失败, 判定过程出错", context.message)
            self.assertIn("范围: 群 产品群 (p)", context.message)
            self.assertIn("消息: m", context.message)
            with closing(queue._connect()) as db:
                result = db.execute("SELECT code, status, summary FROM notification_events").fetchone()
                self.assertEqual(tuple(result)[:2], ("model_failed", "failed"))
                self.assertEqual(result["summary"], context.message)
            record = queue.list_notifications()[0]
            self.assertEqual(record["summary"], context.message)

    async def test_revoked_notification_authorization_does_not_send(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = DryRunQueue(Path(temp) / "state.sqlite")
            queue.initialize()
            class Context:
                async def send_message(self, target, message):
                    raise AssertionError("revoked notification was sent")
            dispatcher = build_notification_dispatcher(context=Context(), queue=queue,
                recipients=["p:FriendMessage:admin"], interval_seconds=60,
                message_chain_factory=lambda text: text, is_authorized=lambda: False)
            dispatcher.start()
            dispatcher.offer("model_failed")
            await dispatcher.close()
            with closing(queue._connect()) as db:
                self.assertEqual(db.execute("SELECT status FROM notification_events").fetchone()[0], "failed")

    async def test_revocation_prevents_later_batches(self):
        authorized = True
        sent = []
        class Context:
            async def send_message(self, target, message):
                nonlocal authorized
                sent.append(target)
                authorized = False
                return True
        with tempfile.TemporaryDirectory() as temp:
            queue = DryRunQueue(Path(temp) / "state.sqlite")
            queue.initialize()
            targets = [f"p:FriendMessage:{index}" for index in range(5)]
            dispatcher = build_notification_dispatcher(context=Context(), queue=queue, recipients=targets,
                interval_seconds=60, message_chain_factory=lambda text: text, is_authorized=lambda: authorized)
            dispatcher.start()
            dispatcher.offer("queue_full")
            await dispatcher.close()
            self.assertEqual(sent, targets[:1])
            with closing(queue._connect()) as db:
                self.assertCountEqual(
                    [row[0] for row in db.execute("SELECT status FROM notification_events")],
                    ["submitted"] + ["failed"] * 4,
                )

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
            def record_notification(self, code, status, summary=""):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("audit test timed out")
        class Context:
            async def send_message(self, target, message):
                return True
        dispatcher = build_notification_dispatcher(context=Context(), queue=Queue(),
            recipients=["p:FriendMessage:admin"], interval_seconds=60, message_chain_factory=lambda text: text)
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
