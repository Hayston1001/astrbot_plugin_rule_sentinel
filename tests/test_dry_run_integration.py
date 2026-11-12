import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from sentinel.dry_run_ingest import DryRunAdmission, IncomingMessage
from sentinel.dry_run_queue import DryRunQueue
from sentinel.dry_run_runtime import DryRunRuntime
from sentinel.dry_run_worker import DryRunWorker
from sentinel.event_intake import EventIntake
from sentinel.framework_session_gate import FrameworkSessionGate
from sentinel.llm_decider import LlmDecider
from sentinel.monitoring_policy import MonitoringPolicy
from sentinel.policy_guard import PolicyGuard
from sentinel.rule_store import RuleStore


class DryRunIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_buffered_messages_recheck_revoked_session_without_blocking_ingress(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite"
            rules, queue = RuleStore(path), DryRunQueue(path)
            rules.initialize()
            queue.initialize()
            rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                       text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                       severity_seconds={"low": 60}, modified_by="admin")
            allowed = [True]
            async def session(origin):
                return allowed[0]
            async def plugin(origin, name):
                return True
            gate = FrameworkSessionGate(get_config=lambda origin: {"platform_settings": {
                "enable_id_white_list": True, "id_whitelist": ["g"]}},
                is_session_enabled=session, is_plugin_enabled=plugin, plugin_name="plugin")
            policy = MonitoringPolicy("allowlist", frozenset({("p", "g")}))
            admission = DryRunAdmission(queue, rules, policy=policy, capacity=10, model="synthetic", session_gate=gate)
            started, release = asyncio.Event(), asyncio.Event()
            calls = []
            async def invoke(*, model, message, rules, strategy, language):
                calls.append(message)
                if message == "first private message":
                    started.set()
                    await release.wait()
                return SimpleNamespace(role="assistant", is_chunk=False, completion_text="NO_ACTION",
                                       tools_call_name=[], tools_call_args=[])
            decider = LlmDecider(rules, invoke, model="synthetic", max_duration_seconds=300, queue=queue)
            worker = DryRunWorker(queue, PolicyGuard(decider, lambda: policy, session_gate=gate),
                                  concurrency=1, timeout_seconds=5)
            runtime = DryRunRuntime(admission, worker)
            await runtime.start_after_exclusive_stop()
            intake = EventIntake(runtime, queue, capacity=10)
            intake.start()
            def message(message_id, text):
                return IncomingMessage("p", "g", message_id, "sender", None, text, True, False,
                                       sender_is_verified_non_bot=True, session_origin="p:GroupMessage:g")
            try:
                self.assertEqual(intake.offer(message("first", "first private message")), "accepted")
                await asyncio.wait_for(started.wait(), timeout=2)
                self.assertEqual(intake.offer(message("queued", "queued private message")), "accepted")
                await asyncio.wait_for(intake.inbox.join(), timeout=2)
                allowed[0] = False
                self.assertEqual(intake.offer(message("denied", "denied private message")), "accepted")
                await asyncio.wait_for(intake.inbox.join(), timeout=2)
                release.set()
                await asyncio.wait_for(asyncio.gather(*list(runtime._tasks.values())), timeout=2)
                allowed[0] = True
                self.assertEqual(intake.offer(message("restored", "restored private message")), "accepted")
                await asyncio.wait_for(intake.inbox.join(), timeout=2)
                await asyncio.wait_for(asyncio.gather(*list(runtime._tasks.values())), timeout=2)
            finally:
                release.set()
                await intake.close()
                await runtime.close()
            events = queue.list_events(platform_id="p", group_id="g")
            self.assertEqual([(row["message_id"], row["status"]) for row in events],
                [("first", "started"), ("first", "review"), ("queued", "skipped"),
                 ("restored", "started"), ("restored", "no_action")])
            self.assertEqual(json.loads(events[1]["detail"])["review_reason"], "framework_session_denied")
            self.assertEqual(events[2]["detail"], "framework_session_denied")
            self.assertEqual(calls, ["first private message", "restored private message"])
            self.assertFalse(any("private message" in row["detail"] for row in events))
            self.assertEqual(intake.counts["framework_session_denied"], 1)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM dry_run_queue WHERE message_id='denied'").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM dry_run_queue WHERE payload IS NOT NULL").fetchone()[0], 0)

    async def test_message_to_audit_without_chat_or_platform_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            rules = RuleStore(path)
            queue = DryRunQueue(path)
            rules.initialize()
            queue.initialize()
            rules.save(platform_id="qq", group_id="g", rule_id="r", expected_version=0,
                       text="No spam", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                       severity_seconds={"low": 60}, modified_by="admin")
            async def invoke(**kwargs):
                return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["propose_action"],
                    tools_call_args=[{"violated": True, "rule_id": "r", "severity": "low", "reason": "violates rule"}])
            policy = MonitoringPolicy("allowlist", frozenset({("qq", "g")}))
            admission = DryRunAdmission(queue, rules, policy=policy, capacity=100, model="model")
            decider = LlmDecider(rules, invoke, model="model", max_duration_seconds=600, queue=queue)
            worker = DryRunWorker(queue, decider, concurrency=1, timeout_seconds=2)
            runtime = DryRunRuntime(admission, worker)
            await runtime.start_after_exclusive_stop()
            message = IncomingMessage("qq", "g", "1", "sender", "bot", "hello", True, False)
            self.assertEqual(await runtime.submit(message), "queued")
            await asyncio.gather(*list(runtime._tasks.values()))
            await runtime.close()
            events = queue.list_events(platform_id="qq", group_id="g")
            self.assertEqual([event["status"] for event in events], ["started", "proposed"])
            self.assertEqual(events[0]["attempt_id"], events[1]["attempt_id"])
            detail = json.loads(events[1]["detail"])
            self.assertEqual(detail["proposal"]["target_sender_id"], "sender")
            self.assertEqual(detail["proposal"]["duration_seconds"], 60)
            self.assertNotIn("hello", events[1]["detail"])

    async def test_runtime_restarts_pending_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            queue = DryRunQueue(path)
            queue.initialize()
            queue.enqueue(platform_id="p", group_id="g", message_id="1", payload={}, capacity=10)
            item = queue.claim("p", "g")
            self.assertIsNotNone(item)
            class Worker:
                def __init__(self):
                    self.queue = queue
                    self.started = asyncio.Event()
                async def drain_group(self, platform_id, group_id):
                    self.started.set()
            worker = Worker()
            runtime = DryRunRuntime(None, worker)
            await runtime.start_after_exclusive_stop()
            await asyncio.wait_for(worker.started.wait(), timeout=1)
            await runtime.close()
            self.assertIsNotNone(queue.claim("p", "g"))
