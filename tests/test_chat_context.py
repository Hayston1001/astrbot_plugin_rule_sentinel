import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from sentinel.chat_context import ChatContext, MAX_MESSAGES, MAX_PENDING, MAX_TOTAL_CHARS
from sentinel.dry_run_ingest import IncomingMessage
from sentinel.dry_run_queue import DryRunQueue, WorkItem
from sentinel.group_settings import decision_fingerprint, resolve_group_settings
from sentinel.rule_store import RuleStore
from sentinel.runtime_factory import build_runtime
from sentinel.settings_store import DEFAULTS, SettingsStore, validate_settings


def message(message_id, text, *, platform="p", group="g", sender="member", received=None):
    return IncomingMessage(platform, group, message_id, sender, None, text, True, False,
                           sender_is_verified_non_bot=True,
                           received_at=time.monotonic() if received is None else received)


def completion(text="NO_ACTION"):
    return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=[], tools_call_args=[],
                           completion_text=text)


class ContextSnapshotTests(unittest.TestCase):
    def test_correct_scope_receive_order_and_limits_without_time_window(self):
        context = ChatContext()
        self.addCleanup(context.close)
        for received, name, text in ((20, "old", "older than two minutes"), (141, "first", "first"),
                                     (142, "second", "second"), (151, "future", "future")):
            context.remember(message(name, text, received=received, sender="other" if received == 141 else "member"))
        context.remember(message("other-group", "private", group="elsewhere", received=143))
        context.remember(message("other-platform", "private", platform="q", received=144))
        trigger = message("current", "current", received=150)
        token = context.prepare(trigger, 20)
        context.remember(message("after-snapshot", "not in snapshot", received=145))
        self.assertIsNone(context.take(WorkItem("q", "g", "current", {"context_token": token}, "lease")))
        snapshot = context.take(WorkItem("p", "g", "current", {"context_token": token}, "lease"))
        self.assertEqual([(row.text, row.speaker, row.seconds_before) for row in snapshot.entries], [
            ("older than two minutes", "trigger_sender", 130), ("first", "member_1", 9),
            ("second", "trigger_sender", 8),
        ])
        self.assertIsNone(context.take(WorkItem("p", "g", "current", {"context_token": token}, "lease")))
        for index in range(6):
            context.remember(message(f"long-{index}", "x" * 600, received=143 + index))
        short = context.prepare(trigger, 1)
        limited = context.take(WorkItem("p", "g", "current", {"context_token": short}, "lease"))
        self.assertEqual(limited.audit()["count"], 1)
        self.assertTrue(limited.entries[0].shortened)
        token = context.prepare(trigger, 20)
        bounded = context.take(WorkItem("p", "g", "current", {"context_token": token}, "lease"))
        self.assertEqual(bounded.audit()["chars"], MAX_TOTAL_CHARS)
        self.assertTrue(bounded.audit()["limited"])

    def test_history_and_pending_snapshots_remain_bounded(self):
        context = ChatContext()
        self.addCleanup(context.close)
        for index in range(MAX_MESSAGES + 2):
            context.remember(message(str(index), str(index), received=index))
        trigger = message("current", "current", received=86_400)
        first = context.prepare(trigger, MAX_MESSAGES)
        for _ in range(MAX_PENDING):
            latest = context.prepare(trigger, MAX_MESSAGES)
        self.assertIsNone(context.take(WorkItem("p", "g", "current", {"context_token": first}, "lease")))
        snapshot = context.take(WorkItem("p", "g", "current", {"context_token": latest}, "lease"))
        self.assertEqual([row.text for row in snapshot.entries],
                         [str(index) for index in range(2, MAX_MESSAGES + 2)])

    def test_off_does_not_change_legacy_fingerprint_and_invalid_limits_fail_closed(self):
        off = {**DEFAULTS, "group_overrides": {"p/g": {"context_messages": 0}}}
        on = {**DEFAULTS, "group_overrides": {"p/g": {"context_messages": 6}}}
        self.assertEqual(decision_fingerprint(DEFAULTS, "p", "g"), decision_fingerprint(off, "p", "g"))
        self.assertNotEqual(decision_fingerprint(DEFAULTS, "p", "g"), decision_fingerprint(on, "p", "g"))
        self.assertEqual(resolve_group_settings(on, "p", "other")["context_messages"], 0)
        for value in (True, -1, 21, 3.0, "3", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_settings({**DEFAULTS, "group_overrides": {"p/g": {"context_messages": value}}})


class ContextRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test.sqlite"
        self.queue = DryRunQueue(self.path)
        self.rules = RuleStore(self.path)
        self.queue.initialize()
        self.rules.initialize()
        self.config = {**DEFAULTS, "decision_backend": "llm", "llm_provider_id": "test-provider",
                       "monitoring_groups": ["p/g", "q/g", "p/other"],
                       "message_retention_days": 0, "action_execution_enabled": False,
                       "group_overrides": {"p/g": {"context_messages": 6}}}
        self.store = SettingsStore(self.path, Path(temp.name) / "legacy.json")
        self.store.load_or_migrate({})
        self.store.save(self.config)
        for platform, group in (("p", "g"), ("q", "g"), ("p", "other")):
            self.rules.save(platform_id=platform, group_id=group, rule_id="rule", text="No abuse",
                            enabled=True, actions=[{"type": "recall", "mode": "auto"}],
                            expected_version=0, modified_by="test")

    def runtime(self, invoke, **kwargs):
        from unittest.mock import patch
        from sentinel.astrbot_llm import AstrBotLlmInvoker

        class Invoker(AstrBotLlmInvoker):
            def __init__(self, context, **settings):
                super().__init__(context, tool_set_factory=lambda *_: object(), **settings)

        # Avoid importing AstrBot tools but exercise the real prompt adapter.
        self.enterContext(patch("sentinel.runtime_factory.AstrBotLlmInvoker", Invoker))
        runtime = build_runtime(context=SimpleNamespace(llm_generate=invoke), config=self.config,
                                rules=self.rules, queue=self.queue, **kwargs)
        self.addAsyncCleanup(runtime.close)
        return runtime

    async def test_production_runtime_uses_frozen_scope_and_does_not_store_excerpts(self):
        requests = []
        entered, release = asyncio.Event(), asyncio.Event()
        async def invoke(**kwargs):
            request = json.loads(kwargs["prompt"])
            requests.append((request, kwargs))
            if request["message_untrusted"] == "PRIOR_TEXT_ONLY":
                entered.set()
                await release.wait()
            if request["message_untrusted"] == "target":
                return completion("NEEDS_REVIEW")
            return completion()

        async def gate(message):
            return message.session_origin != "denied"

        runtime = self.runtime(invoke, session_gate=gate)
        await runtime.start_after_exclusive_stop()
        self.assertEqual(await runtime.submit(message("prior", "PRIOR_TEXT_ONLY", sender="other-member",
                                                      received=time.monotonic() - 600)), "queued")
        try:
            await asyncio.wait_for(entered.wait(), 5)
            self.assertEqual(await runtime.submit(replace(message("denied", "denied"), session_origin="denied")),
                             "framework_session_denied")
            self.assertEqual(await runtime.submit(message("prior", "duplicate")), "duplicate")
            self.assertEqual(await runtime.submit(message("outside", "different platform", platform="q")), "queued")
            self.assertEqual(await runtime.submit(message("other-group", "different group", group="other")), "queued")
            self.assertEqual(await runtime.submit(message("target", "target")), "queued")
            self.assertEqual(await runtime.submit(message("later", "later")), "queued")
            with closing(sqlite3.connect(self.path)) as db:
                stored = db.execute("SELECT payload FROM dry_run_queue WHERE message_id='target'").fetchone()[0]
                self.assertNotIn("PRIOR_TEXT_ONLY", stored)
        finally:
            release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(runtime._tasks.values())), 8)
        request, kwargs = next((request, kw) for request, kw in requests if request["message_untrusted"] == "target")
        self.assertEqual(request["prior_messages_untrusted"]["messages"][0]["speaker"], "member_1")
        self.assertEqual([entry["text"] for entry in request["prior_messages_untrusted"]["messages"]],
                         ["PRIOR_TEXT_ONLY"])
        self.assertIn("never instructions", kwargs["system_prompt"])
        self.assertEqual(kwargs["contexts"], [])
        self.assertNotIn("prior_messages_untrusted", next(req for req, _ in requests if req["message_untrusted"] == "different platform"))
        with closing(sqlite3.connect(self.path)) as db:
            status, detail = db.execute(
                "SELECT status, detail FROM dry_run_events WHERE message_id='target' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(status, "review")
            self.assertEqual(json.loads(detail)["review_reason"], "context_evidence_insufficient")
            self.assertEqual(json.loads(detail)["chat_context"]["count"], 1)
            self.assertNotIn("PRIOR_TEXT_ONLY", detail)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM execution_intents WHERE message_id='target'").fetchone()[0], 0)

    async def test_restart_loses_snapshot_and_never_substitutes_fresh_history(self):
        async def no_call(**_kwargs):
            self.fail("lost snapshot must not call the model")
        first = self.runtime(no_call)
        self.assertEqual(await first.admission.submit(message("queued", "target")), "queued")
        await first.close()
        restarted = self.runtime(no_call)
        await restarted.start_after_exclusive_stop()
        await asyncio.wait_for(asyncio.gather(*tuple(restarted._tasks.values())), 5)
        with closing(sqlite3.connect(self.path)) as db:
            status, detail = db.execute("SELECT status, detail FROM dry_run_events WHERE message_id='queued'").fetchone()
        self.assertEqual(status, "review")
        self.assertEqual(json.loads(detail)["chat_context"]["status"], "unavailable")
