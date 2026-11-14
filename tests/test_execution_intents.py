import json
import asyncio
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from datetime import datetime, timedelta, timezone

from sentinel.dry_run_queue import DryRunQueue
from sentinel.mute_execution_store import MuteExecutionStore
from sentinel.mute_executor import MuteExecutor
from sentinel.rule_store import RuleStore
from sentinel.settings_store import SettingsStore
from sentinel.qq_mute_platform import QQMutePlatform
from test_qq_mute_platform import FakeResponse, FakeSession


class ExecutionIntentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "state.sqlite"
        self.rules, self.queue = RuleStore(self.path), DryRunQueue(self.path)
        self.rules.initialize()
        self.queue.initialize()
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                        text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                        severity_seconds={"low": 60}, modified_by="admin")
        self.queue.enqueue(platform_id="p", group_id="g", message_id="m", payload={"text": "private", "sender_id": "sender"}, capacity=10)
        self.item = self.queue.claim("p", "g")
        self.detail = json.dumps({"proposal": {"action": "mute", "rule_id": "r", "rule_version": 1,
            "target_sender_id": "sender", "duration_seconds": 60, "mode": "auto"}})

    def test_intent_and_audit_commit_together_before_payload_erasure(self):
        self.assertTrue(self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),)))
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT target_sender_id, duration_seconds, expires_at, status FROM execution_intents").fetchone()
            self.assertEqual(row[:2], ("sender", 60))
            self.assertTrue(row[2].endswith("+00:00"))
            self.assertEqual(row[3], "inactive")
            self.assertIsNone(db.execute("SELECT payload FROM dry_run_queue").fetchone()[0])
            self.assertEqual(db.execute("SELECT count(*) FROM dry_run_events WHERE status='proposed'").fetchone()[0], 1)

    def test_mute_intent_preserves_valid_message_time_for_overview(self):
        message_time = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE dry_run_queue SET payload=? WHERE state='processing'", (json.dumps({
                "text": "private", "sender_id": "sender", "message_time": message_time}),))
            db.commit()
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        with closing(sqlite3.connect(self.path)) as db:
            saved = db.execute("SELECT message_time FROM execution_intents").fetchone()[0]
        self.assertEqual(datetime.fromisoformat(saved), datetime.fromisoformat(message_time))

    def test_failed_intent_write_rolls_back_terminal_audit_and_payload(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER fail_intent BEFORE INSERT ON execution_intents BEGIN SELECT RAISE(ABORT, 'blocked'); END")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM dry_run_events WHERE status='proposed'").fetchone()[0], 0)
            state, payload = db.execute("SELECT state, payload FROM dry_run_queue").fetchone()
            self.assertEqual(state, "processing")
            self.assertIn("private", payload)

    def test_plan_creates_one_ordered_intent_per_configured_action(self):
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=1,
                        text="rule", enabled=True, actions=[
                            {"type": "recall", "mode": "auto"},
                            {"type": "reply", "mode": "confirm", "template": "notice"},
                            {"type": "mute", "mode": "auto", "duration_mode": "strict", "severity_seconds": {"low": 60}},
                        ], modified_by="admin")
        detail = json.dumps({"proposal": {"rule_id": "r", "rule_version": 2,
                            "target_sender_id": "sender", "duration_seconds": 60}})
        self.assertTrue(self.queue.finish(self.item, status="proposed", detail=detail, rule_versions=(("r", 2),)))
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT action_index, action, mode, duration_seconds, plan_id FROM execution_intents ORDER BY action_index").fetchall()
        self.assertEqual([row[:4] for row in rows], [(0, "recall", "auto", None),
                         (1, "reply", "confirm", None), (2, "mute", "auto", 60)])
        self.assertEqual(len({row[4] for row in rows}), 1)

    def test_plan_executes_automatic_actions_in_order_after_blocked_recall(self):
        from sentinel.dry_run_host import DryRunHost
        self.enable_execution()
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=1,
                        text="rule", enabled=True, actions=[
                            {"type": "recall", "mode": "auto"},
                            {"type": "reply", "mode": "auto", "template": "rule {rule_id}"},
                            {"type": "mute", "mode": "auto", "duration_mode": "strict", "severity_seconds": {"low": 60}},
                        ], modified_by="admin")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE dry_run_queue SET payload=? WHERE state='processing'", (json.dumps({
                "text": "private", "sender_id": "sender", "message_time": datetime.now(timezone.utc).isoformat()}),))
            db.commit()
        detail = json.dumps({"proposal": {"rule_id": "r", "rule_version": 2,
                            "target_sender_id": "sender", "duration_seconds": 60}})
        self.queue.finish(self.item, status="proposed", detail=detail, rule_versions=(("r", 2),))
        calls = []
        class Platform:
            async def verify_permission(self, *args): return True
            async def recall(self, *args):
                calls.append("recall")
                return {"outcome": "blocked", "reason_code": "protected_target", "platform_code": "40062003"}
            async def send_group_message(self, *args, **kwargs):
                calls.append("reply")
                self_args = (args, kwargs)
                assert self_args[0][2] == "rule r" and self_args[1]["reference_id"] == "m"
                return {"outcome": "succeeded", "platform_code": "200", "trace_id": "reply-trace"}
            async def mute(self, *args):
                calls.append("mute")
                return {"outcome": "succeeded"}
        async def run(platform):
            host = DryRunHost(context=None, config={}, rules=None, queue=self.queue, lock_path="unused")
            host.mute_executor = MuteExecutor(MuteExecutionStore(self.path), platform)
            task = await host._schedule_plan(1)
            await task
        asyncio.run(run(Platform()))
        self.assertEqual(calls, ["recall", "reply", "mute"])
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT action, status, attempts, trace_id FROM execution_intents ORDER BY action_index").fetchall()
        self.assertEqual([(row[0], row[1], row[2]) for row in rows],
                         [("recall", "blocked", 1), ("reply", "succeeded", 1), ("mute", "succeeded", 1)])
        self.assertEqual(rows[1][3], "reply-trace")
        self.assertEqual(self.queue.list_messages(platform_id="p", group_id="g")[0]["execution_summary"], "partial_failed")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM execution_attempts")
            db.execute("UPDATE execution_intents SET status='pending_auto', attempts=0, platform_code=NULL, reason_code=NULL, trace_id=NULL")
            db.commit()
        calls.clear()
        class SuccessfulPlatform(Platform):
            async def recall(self, *args):
                calls.append("recall")
                return {"outcome": "succeeded"}
        asyncio.run(run(SuccessfulPlatform()))
        self.assertEqual(calls, ["recall", "reply", "mute"])
        record = self.queue.list_messages(platform_id="p", group_id="g")[0]
        self.assertEqual([item["action"] for item in record["executions"]], calls)
        self.assertEqual(record["execution_summary"], "succeeded")

    def test_reply_failure_does_not_prevent_later_mute(self):
        from sentinel.dry_run_host import DryRunHost
        self.enable_execution()
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=1,
                        text="rule", enabled=True, actions=[
                            {"type": "reply", "mode": "auto", "template": "notice"},
                            {"type": "mute", "mode": "auto", "duration_mode": "strict", "severity_seconds": {"low": 60}},
                        ], modified_by="admin")
        detail = json.dumps({"proposal": {"rule_id": "r", "rule_version": 2,
                            "target_sender_id": "sender", "duration_seconds": 60}})
        self.queue.finish(self.item, status="proposed", detail=detail, rule_versions=(("r", 2),))
        calls = []
        class Platform:
            async def verify_permission(self, *args): return True
            async def send_group_message(self, *args, **kwargs):
                calls.append(("reply", kwargs["markdown"]))
                return {"outcome": "failed", "platform_code": "400"}
            async def mute(self, *args):
                calls.append(("mute", None))
                return {"outcome": "succeeded"}
        async def sleep(delay): pass
        async def run():
            host = DryRunHost(context=None, config={}, rules=None, queue=self.queue, lock_path="unused")
            host.mute_executor = MuteExecutor(MuteExecutionStore(self.path), Platform(), sleep=sleep)
            await (await host._schedule_plan(1))
        asyncio.run(run())
        self.assertEqual(calls, [("reply", True), ("reply", False), ("mute", None)])
        row = self.queue.list_messages(platform_id="p", group_id="g")[0]
        self.assertEqual([item["status"] for item in row["executions"]], ["failed", "succeeded"])
        self.assertEqual(row["execution_summary"], "partial_failed")

    def test_confirm_action_waits_while_later_auto_action_runs(self):
        from sentinel.dry_run_host import DryRunHost
        self.enable_execution()
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=1,
                        text="rule", enabled=True, actions=[
                            {"type": "reply", "mode": "confirm", "template": "notice"},
                            {"type": "mute", "mode": "auto", "duration_mode": "strict", "severity_seconds": {"low": 60}},
                        ], modified_by="admin")
        detail = json.dumps({"proposal": {"rule_id": "r", "rule_version": 2,
                            "target_sender_id": "sender", "duration_seconds": 60}})
        self.queue.finish(self.item, status="proposed", detail=detail, rule_versions=(("r", 2),))
        calls = []
        class Platform:
            async def verify_permission(self, *args): return True
            async def send_group_message(self, *args, **kwargs):
                calls.append("reply")
                return {"outcome": "succeeded"}
            async def mute(self, *args):
                calls.append("mute")
                return {"outcome": "succeeded"}
        async def run():
            host = DryRunHost(context=None, config={}, rules=None, queue=self.queue, lock_path="unused")
            host.mute_executor = MuteExecutor(MuteExecutionStore(self.path), Platform())
            await (await host._schedule_plan(2))
            self.assertEqual(calls, ["mute"])
            row = await asyncio.to_thread(self.queue.list_messages, platform_id="p", group_id="g")
            self.assertEqual(row[0]["execution_summary"], "pending_confirm")
            self.assertEqual(await host.confirm_mute(1), "succeeded")
            self.assertEqual(await host.confirm_mute(1), "not_claimed")
        asyncio.run(run())
        self.assertEqual(calls, ["mute", "reply"])

    def test_model_cannot_replace_trusted_sender(self):
        bad = json.loads(self.detail)
        bad["proposal"]["target_sender_id"] = "other"
        with self.assertRaises(ValueError):
            self.queue.finish(self.item, status="proposed", detail=json.dumps(bad), rule_versions=(("r", 1),))

    def enable_execution(self):
        settings = SettingsStore(self.path, self.path.parent / "backup.json")
        config = settings.load_or_migrate({})
        config.update(action_execution_enabled=True, monitoring_groups=["p/g"],
                      decision_backend="llm", llm_provider_id="model")
        settings.save(config)

    def test_atomic_claim_and_retry_reuse_fixed_expiry(self):
        self.enable_execution()
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        store = MuteExecutionStore(self.path)
        first = store.begin_attempt(1, requested_mode="auto")
        self.assertEqual(first["attempt"], 1)
        self.assertIsNone(store.begin_attempt(1, requested_mode="auto"))
        self.assertEqual(store.complete_attempt(1, 1, outcome="failed", platform_code="400"), "pending_retry")
        self.assertIsNone(store.begin_attempt(1, requested_mode="auto"))
        second = store.begin_attempt(1, requested_mode="auto", allow_retry=True)
        self.assertEqual(second["expires_at"], first["expires_at"])
        self.assertEqual(store.complete_attempt(1, 2, outcome="failed", platform_code="400"), "pending_retry")
        third = store.begin_attempt(1, requested_mode="auto", allow_retry=True)
        self.assertEqual(third["expires_at"], first["expires_at"])
        self.assertEqual(store.complete_attempt(1, 3, outcome="failed", platform_code="400"), "failed")
        self.assertIsNone(store.begin_attempt(1, requested_mode="auto"))

    def test_unknown_is_not_retried(self):
        self.enable_execution()
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        store = MuteExecutionStore(self.path)
        store.begin_attempt(1, requested_mode="auto")
        self.assertEqual(store.complete_attempt(1, 1, outcome="unknown"), "unknown")
        self.assertIsNone(store.begin_attempt(1, requested_mode="auto"))

    def test_confirmation_claim_is_idempotent_under_concurrency(self):
        self.enable_execution()
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=1,
                        text="rule", enabled=True, actions=[{"type": "mute", "mode": "confirm",
                        "duration_mode": "strict", "severity_seconds": {"low": 60}}], modified_by="admin")
        detail = json.loads(self.detail)
        detail["proposal"].update(rule_version=2, mode="confirm")
        self.queue.finish(self.item, status="proposed", detail=json.dumps(detail), rule_versions=(("r", 2),))
        store = MuteExecutionStore(self.path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: store.begin_attempt(1, requested_mode="confirm"), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM execution_attempts").fetchone()[0], 1)

    def test_confirm_waits_for_click_and_double_click_sends_once(self):
        self.enable_execution()
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=1,
                        text="rule", enabled=True, actions=[{"type": "mute", "mode": "confirm",
                        "duration_mode": "strict", "severity_seconds": {"low": 60}}], modified_by="admin")
        detail = json.loads(self.detail)
        detail["proposal"].update(rule_version=2, mode="confirm")
        self.queue.finish(self.item, status="proposed", detail=json.dumps(detail), rule_versions=(("r", 2),))
        calls = []
        class FakePlatform:
            async def verify_permission(self, *args):
                return True
            async def mute(self, *args):
                calls.append(args)
                await asyncio.sleep(0)
                return {"outcome": "succeeded"}
        executor = MuteExecutor(MuteExecutionStore(self.path), FakePlatform())
        self.assertEqual(calls, [])
        async def both():
            return await asyncio.gather(executor.execute(1, mode="confirm"), executor.execute(1, mode="confirm"))
        results = asyncio.run(both())
        self.assertCountEqual(results, ["succeeded", "not_claimed"])
        self.assertEqual(len(calls), 1)

    def test_overview_confirms_only_the_selected_mute_without_dispatching_other_actions(self):
        from sentinel.dry_run_host import DryRunHost
        self.enable_execution()
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=1,
                        text="rule", enabled=True, actions=[
                            {"type": "reply", "mode": "confirm", "template": "notice"},
                            {"type": "recall", "mode": "auto"},
                            {"type": "mute", "mode": "confirm", "duration_mode": "strict", "severity_seconds": {"low": 60}},
                        ], modified_by="admin")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE dry_run_queue SET payload=? WHERE state='processing'", (json.dumps({
                "text": "private", "sender_id": "sender", "message_time": datetime.now(timezone.utc).isoformat()}),))
            db.commit()
        detail = json.dumps({"proposal": {"rule_id": "r", "rule_version": 2,
                             "target_sender_id": "sender", "duration_seconds": 60}})
        self.queue.finish(self.item, status="proposed", detail=detail, rule_versions=(("r", 2),))
        calls = []
        class Platform:
            async def verify_permission(self, *args): return True
            async def send_group_message(self, *args, **kwargs):
                calls.append("reply")
                return {"outcome": "succeeded"}
            async def recall(self, *args):
                calls.append("recall")
                return {"outcome": "succeeded"}
            async def mute(self, *args):
                calls.append("mute")
                return {"outcome": "succeeded"}
        async def run():
            host = DryRunHost(context=None, config={}, rules=None, queue=self.queue, lock_path="unused")
            host.mute_executor = MuteExecutor(MuteExecutionStore(self.path), Platform())
            self.assertEqual(await host.confirm_single_mute(1), "not_claimed")
            results = await asyncio.gather(host.confirm_single_mute(3), host.confirm_single_mute(3))
            self.assertCountEqual(results, ["succeeded", "not_claimed"])
        asyncio.run(run())
        self.assertEqual(calls, ["mute"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT action, status FROM execution_intents ORDER BY id").fetchall(),
                             [("reply", "pending_confirm"), ("recall", "pending_auto"), ("mute", "succeeded")])

    def test_overview_rechecks_expired_mute_before_contacting_platform(self):
        from sentinel.dry_run_host import DryRunHost
        self.enable_execution()
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=1,
                        text="rule", enabled=True, actions=[{"type": "mute", "mode": "confirm",
                        "duration_mode": "strict", "severity_seconds": {"low": 60}}], modified_by="admin")
        detail = json.loads(self.detail)
        detail["proposal"].update(rule_version=2, mode="confirm")
        self.queue.finish(self.item, status="proposed", detail=json.dumps(detail), rule_versions=(("r", 2),))
        created = datetime.now(timezone.utc) - timedelta(minutes=2)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE execution_intents SET created_at=?, expires_at=? WHERE id=1",
                       (created.isoformat(), (created + timedelta(seconds=60)).isoformat()))
            db.commit()
        calls = []
        class Platform:
            async def verify_permission(self, *args):
                calls.append("verify")
                return True
            async def mute(self, *args):
                calls.append("mute")
                return {"outcome": "succeeded"}
        async def run():
            host = DryRunHost(context=None, config={}, rules=None, queue=self.queue, lock_path="unused")
            host.mute_executor = MuteExecutor(MuteExecutionStore(self.path), Platform())
            return await host.confirm_single_mute(1)
        self.assertEqual(asyncio.run(run()), "not_claimed")
        self.assertEqual(self.queue.confirmation_state(1), {"status": "review", "reason_code": "intent_expired"})
        self.assertEqual(calls, [])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM execution_attempts").fetchone()[0], 0)

    def test_changed_rule_requires_review(self):
        self.enable_execution()
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=1,
                        text="changed", enabled=False, allowed_actions=["mute"], duration_mode="strict",
                        severity_seconds={"low": 60}, modified_by="admin")
        self.assertIsNone(MuteExecutionStore(self.path).begin_attempt(1, requested_mode="auto"))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM execution_intents").fetchone()[0], "review")

    def test_executor_retries_only_definite_failures(self):
        self.enable_execution()
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        seen, pauses = [], []
        path = self.path
        class FakePlatform:
            async def verify_permission(self, platform_id, group_id):
                return True
            async def mute(self, platform_id, group_id, sender_id, expires_at):
                with closing(sqlite3.connect(path)) as db:
                    self_status = db.execute("SELECT status FROM execution_attempts ORDER BY attempt DESC LIMIT 1").fetchone()[0]
                    assert self_status == "intent"
                seen.append((sender_id, expires_at))
                return {"outcome": "failed" if len(seen) < 3 else "succeeded", "platform_code": "200"}
        async def sleep(delay):
            pauses.append(delay)
        result = asyncio.run(MuteExecutor(MuteExecutionStore(self.path), FakePlatform(), sleep=sleep).execute(1, mode="auto"))
        self.assertEqual(result, "succeeded")
        self.assertEqual(pauses, [1, 2])
        self.assertEqual(len(set(seen)), 1)
        self.assertEqual(seen[0][0], "sender")

    def test_executor_unknown_does_not_repeat(self):
        self.enable_execution()
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        calls = []
        class FakePlatform:
            async def verify_permission(self, platform_id, group_id):
                return True
            async def mute(self, *args):
                calls.append(args)
                raise TimeoutError("secret")
        result = asyncio.run(MuteExecutor(MuteExecutionStore(self.path), FakePlatform()).execute(1, mode="auto"))
        self.assertEqual(result, "unknown")
        self.assertEqual(len(calls), 1)

    def test_protected_target_is_persisted_and_never_retried(self):
        self.enable_execution()
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        session = FakeSession()
        session.mute_status, session.mute_data = 400, {"code": 40103004}
        published = []
        self.queue.event_sink = lambda *args: published.append(args)
        executor = MuteExecutor(MuteExecutionStore(self.path, notify=self.queue.publish_intent),
                                QQMutePlatform(session, lambda _: ("app", "secret")))
        self.assertEqual(asyncio.run(executor.execute(1, mode="auto")), "blocked")
        self.assertEqual(sum(url.endswith("restrict_chat_setting") for _, url, _ in session.calls), 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status, attempts, platform_code, reason_code FROM execution_intents").fetchone(),
                             ("blocked", 1, "40103004", "protected_target"))
            self.assertEqual(db.execute("SELECT status, reason_code FROM execution_attempts").fetchone(), ("blocked", "protected_target"))
        self.assertGreaterEqual(len(published), 2)
        row = self.queue.message_at_event(*published[-1])
        self.assertEqual(row["execution"]["status"], "blocked")
        self.assertEqual(row["execution"]["reason_code"], "protected_target")

    def test_protected_notice_sends_and_keeps_blocked(self):
        self.enable_execution()
        settings = SettingsStore(self.path, self.path.parent / "backup.json")
        config = settings.load_or_migrate({})
        config.update(protected_target_policy="group_message", protected_target_template="{sender_id} / {rule_id} / {action}",
                      protected_target_quote=True)
        settings.save(config)
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        sent = []
        published = []
        self.queue.event_sink = lambda *args: published.append(args)
        class FakePlatform:
            async def verify_permission(self, *args): return True
            async def mute(self, *args):
                return {"outcome": "blocked", "reason_code": "protected_target", "platform_code": "40103004"}
            async def send_group_message(self, *args, **kwargs):
                sent.append((args, kwargs))
                return {"outcome": "succeeded", "platform_code": "200", "trace_id": "trace-1"}
        result = asyncio.run(MuteExecutor(MuteExecutionStore(self.path, notify=self.queue.publish_intent), FakePlatform()).execute(1, mode="auto"))
        self.assertEqual(result, "blocked")
        self.assertEqual(sent[0][0], ("p", "g", "sender / r / mute"))
        self.assertEqual(sent[0][1]["message_id"], "m")
        self.assertEqual(sent[0][1]["reference_id"], "m")
        self.assertGreater(sent[0][1]["msg_seq"], 0)
        self.assertTrue(sent[0][1]["markdown"])
        self.assertEqual(len(sent), 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status, reason_code FROM execution_intents").fetchone(), ("blocked", "protected_target"))
            self.assertEqual(db.execute("SELECT status, attempts, platform_code, trace_id FROM protected_target_notices").fetchone(),
                             ("succeeded", 1, "200", "trace-1"))
        row = self.queue.message_at_event(*published[-1])
        self.assertEqual(row["execution"]["notice"]["status"], "succeeded")
        self.assertEqual(row["execution"]["notice"]["trace_id"], "trace-1")

    def test_group_notice_override_and_monitoring_recheck(self):
        self.enable_execution()
        settings = SettingsStore(self.path, self.path.parent / "backup.json")
        config = settings.load_or_migrate({})
        config["group_overrides"] = {"p/g": {"protected_policy": "group_message",
                                                "protected_template": "local {rule_id}", "protected_quote": True}}
        settings.save(config)
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        store = MuteExecutionStore(self.path)
        self.assertIsNotNone(store.begin_attempt(1, requested_mode="auto"))
        store.complete_attempt(1, 1, outcome="blocked", reason_code="protected_target")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT template, quote FROM protected_target_notices").fetchone(),
                             ("local {rule_id}", 1))
        config["monitoring_groups"] = []
        settings.save(config)
        self.assertIsNone(store.begin_notice(1))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM protected_target_notices").fetchone()[0], "inactive")

    def test_protected_notice_retries_definite_failure_only_once(self):
        self.enable_execution()
        settings = SettingsStore(self.path, self.path.parent / "backup.json")
        config = settings.load_or_migrate({})
        config.update(protected_target_policy="group_message", protected_target_template="warning")
        settings.save(config)
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        calls = []
        class FakePlatform:
            async def verify_permission(self, *args): return True
            async def mute(self, *args): return {"outcome": "blocked", "reason_code": "protected_target"}
            async def send_group_message(self, *args, **kwargs):
                calls.append(kwargs)
                return {"outcome": "failed", "platform_code": "400", "trace_id": "trace-2"}
        async def sleep(delay): pass
        result = asyncio.run(MuteExecutor(MuteExecutionStore(self.path), FakePlatform(), sleep=sleep).execute(1, mode="auto"))
        self.assertEqual(result, "blocked")
        self.assertEqual(calls, [{"message_id": None, "reference_id": None, "msg_seq": None, "markdown": True},
                                 {"message_id": None, "reference_id": None, "msg_seq": None, "markdown": False}])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status, attempts, platform_code FROM protected_target_notices").fetchone(), ("failed", 2, "400"))
            self.assertEqual(db.execute("SELECT count(*) FROM protected_target_notice_attempts").fetchone()[0], 2)

    def test_exempt_and_disabled_never_send_notice(self):
        self.enable_execution()
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        class FakePlatform:
            async def verify_permission(self, *args): return True
            async def mute(self, *args): return {"outcome": "blocked", "reason_code": "protected_target"}
            async def send_group_message(self, *args, **kwargs): raise AssertionError("unexpected send")
        store = MuteExecutionStore(self.path)
        self.assertEqual(asyncio.run(MuteExecutor(store, FakePlatform()).execute(1, mode="auto")), "blocked")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM protected_target_notices").fetchone()[0], 0)
        settings = SettingsStore(self.path, self.path.parent / "backup.json")
        config = settings.load_or_migrate({})
        config.update(action_execution_enabled=False, protected_target_policy="group_message", protected_target_template="text")
        settings.save(config)
        now = "2026-01-01T00:00:00+00:00"
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("INSERT INTO protected_target_notices (intent_id, status, template, quote, created_at, updated_at) VALUES (1, 'pending', 'text', 0, ?, ?)", (now, now))
            db.commit()
        asyncio.run(MuteExecutor(store, FakePlatform())._send_notice(1))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status, attempts FROM protected_target_notices").fetchone(), ("inactive", 0))

    def test_unknown_notice_is_not_retried(self):
        self.enable_execution()
        settings = SettingsStore(self.path, self.path.parent / "backup.json")
        config = settings.load_or_migrate({})
        config.update(protected_target_policy="group_message", protected_target_template="text")
        settings.save(config)
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        calls = []
        class FakePlatform:
            async def verify_permission(self, *args): return True
            async def mute(self, *args): return {"outcome": "blocked", "reason_code": "protected_target"}
            async def send_group_message(self, *args, **kwargs):
                calls.append(1)
                raise TimeoutError()
        self.assertEqual(asyncio.run(MuteExecutor(MuteExecutionStore(self.path), FakePlatform()).execute(1, mode="auto")), "blocked")
        self.assertEqual(calls, [1])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status, attempts FROM protected_target_notices").fetchone(), ("unknown", 1))

    def test_disabling_between_notice_attempts_stops_retry(self):
        self.enable_execution()
        settings = SettingsStore(self.path, self.path.parent / "backup.json")
        config = settings.load_or_migrate({})
        config.update(protected_target_policy="group_message", protected_target_template="text")
        settings.save(config)
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        calls = []
        class FakePlatform:
            async def verify_permission(self, *args): return True
            async def mute(self, *args): return {"outcome": "blocked", "reason_code": "protected_target"}
            async def send_group_message(self, *args, **kwargs):
                calls.append(1)
                return {"outcome": "failed"}
        async def sleep(delay):
            config["action_execution_enabled"] = False
            await asyncio.to_thread(settings.save, config)
        self.assertEqual(asyncio.run(MuteExecutor(MuteExecutionStore(self.path), FakePlatform(), sleep=sleep).execute(1, mode="auto")), "blocked")
        self.assertEqual(calls, [1])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status, attempts FROM protected_target_notices").fetchone(), ("inactive", 1))

    def test_notice_attempt_audit_failure_prevents_send(self):
        self.enable_execution()
        settings = SettingsStore(self.path, self.path.parent / "backup.json")
        config = settings.load_or_migrate({})
        config.update(protected_target_policy="group_message", protected_target_template="text")
        settings.save(config)
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER fail_notice BEFORE INSERT ON protected_target_notice_attempts BEGIN SELECT RAISE(ABORT, 'blocked'); END")
            db.commit()
        class FakePlatform:
            async def verify_permission(self, *args): return True
            async def mute(self, *args): return {"outcome": "blocked", "reason_code": "protected_target"}
            async def send_group_message(self, *args, **kwargs): raise AssertionError("unexpected send")
        with self.assertRaises(sqlite3.IntegrityError):
            asyncio.run(MuteExecutor(MuteExecutionStore(self.path), FakePlatform()).execute(1, mode="auto"))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status, reason_code FROM execution_intents").fetchone(), ("blocked", "protected_target"))

    def test_expired_quote_falls_back_once_and_audits_each_attempt(self):
        self.enable_execution()
        settings = SettingsStore(self.path, self.path.parent / "backup.json")
        config = settings.load_or_migrate({})
        config.update(protected_target_policy="group_message", protected_target_template="first {sender_id}\nrule {rule_id}",
                      protected_target_quote=True)
        settings.save(config)
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        class Session(FakeSession):
            def request(self, method, url, **kwargs):
                if url.endswith("restrict_chat_setting"):
                    self.calls.append((method, url, kwargs))
                    return FakeResponse(400, {"code": 40103004})
                if url.endswith("/messages"):
                    self.calls.append((method, url, kwargs))
                    count = sum(call[1].endswith("/messages") for call in self.calls)
                    return FakeResponse(400, {"code": 40034005}) if count == 1 else FakeResponse(200, {"id": "sent"})
                return super().request(method, url, **kwargs)
        session = Session()
        async def sleep(delay): pass
        self.assertEqual(asyncio.run(MuteExecutor(MuteExecutionStore(self.path), QQMutePlatform(session, lambda _: ("app", "secret")), sleep=sleep).execute(1, mode="auto")), "blocked")
        calls = [call[2]["json"] for call in session.calls if call[1].endswith("/messages")]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["msg_id"], "m")
        self.assertGreater(calls[0]["msg_seq"], 0)
        self.assertEqual(calls[0]["msg_type"], 2)
        self.assertEqual(calls[0]["message_reference"], {"message_id": "m"})
        self.assertEqual(calls[0]["markdown"]["content"], "first sender rule r")
        self.assertEqual(calls[1], {"msg_type": 2, "markdown": {"content": "first sender rule r"}, "message_reference": {"message_id": "m"}})
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status, sent_quoted, sent_passive, send_method, downgrade_reason FROM protected_target_notices").fetchone(), ("succeeded", 1, 0, "markdown", "passive_expired"))
            self.assertEqual(db.execute("SELECT status, sent_quoted, sent_passive, send_method, downgrade_reason, reason_code FROM protected_target_notice_attempts ORDER BY attempt").fetchall(),
                             [("failed", 1, 1, "markdown", None, "quote_expired"), ("succeeded", 1, 0, "markdown", "passive_expired", None)])
            self.assertEqual(db.execute("SELECT status FROM execution_intents").fetchone()[0], "blocked")

    def test_quoted_unknown_never_falls_back(self):
        self.enable_execution()
        settings = SettingsStore(self.path, self.path.parent / "backup.json")
        config = settings.load_or_migrate({})
        config.update(protected_target_policy="group_message", protected_target_template="text", protected_target_quote=True)
        settings.save(config)
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        calls = []
        class FakePlatform:
            async def verify_permission(self, *args): return True
            async def mute(self, *args): return {"outcome": "blocked", "reason_code": "protected_target"}
            async def send_group_message(self, *args, **kwargs):
                calls.append(kwargs)
                return {"outcome": "unknown"}
        self.assertEqual(asyncio.run(MuteExecutor(MuteExecutionStore(self.path), FakePlatform()).execute(1, mode="auto")), "blocked")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["message_id"], "m")
        self.assertEqual(calls[0]["reference_id"], "m")
        self.assertGreater(calls[0]["msg_seq"], 0)
        self.assertTrue(calls[0]["markdown"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status, attempts, sent_quoted, downgraded FROM protected_target_notices").fetchone(), ("unknown", 1, 1, 0))

    def test_rejected_reference_after_expired_passive_is_removed_once(self):
        self.enable_execution()
        settings = SettingsStore(self.path, self.path.parent / "backup.json")
        config = settings.load_or_migrate({})
        config.update(protected_target_policy="group_message", protected_target_template="text", protected_target_quote=True)
        settings.save(config)
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        calls = []
        class FakePlatform:
            async def verify_permission(self, *args): return True
            async def mute(self, *args): return {"outcome": "blocked", "reason_code": "protected_target"}
            async def send_group_message(self, *args, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1: return {"outcome": "failed", "reason_code": "quote_expired", "platform_code": "40034005"}
                return {"outcome": "failed" if len(calls) == 2 else "succeeded", "platform_code": "400" if len(calls) == 2 else "200"}
        async def sleep(delay): pass
        self.assertEqual(asyncio.run(MuteExecutor(MuteExecutionStore(self.path), FakePlatform(), sleep=sleep).execute(1, mode="auto")), "blocked")
        self.assertEqual([(c["reference_id"], c["message_id"], c["markdown"]) for c in calls],
                         [("m", "m", True), ("m", None, True), (None, None, True)])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status, attempts, downgrade_reason FROM protected_target_notices").fetchone(), ("succeeded", 3, "reference_rejected"))
            self.assertEqual(db.execute("SELECT downgrade_reason FROM protected_target_notice_attempts ORDER BY attempt").fetchall(),
                             [(None,), ("passive_expired",), ("reference_rejected",)])

    def test_unknown_after_expired_passive_never_removes_reference(self):
        self.enable_execution()
        settings = SettingsStore(self.path, self.path.parent / "backup.json")
        config = settings.load_or_migrate({})
        config.update(protected_target_policy="group_message", protected_target_template="text", protected_target_quote=True)
        settings.save(config)
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        calls = []
        class FakePlatform:
            async def verify_permission(self, *args): return True
            async def mute(self, *args): return {"outcome": "blocked", "reason_code": "protected_target"}
            async def send_group_message(self, *args, **kwargs):
                calls.append(kwargs)
                return {"outcome": "failed", "reason_code": "quote_expired"} if len(calls) == 1 else {"outcome": "unknown"}
        async def sleep(delay): pass
        self.assertEqual(asyncio.run(MuteExecutor(MuteExecutionStore(self.path), FakePlatform(), sleep=sleep).execute(1, mode="auto")), "blocked")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["reference_id"], "m")
        self.assertIsNone(calls[1]["message_id"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status, attempts FROM protected_target_notices").fetchone(), ("unknown", 2))

    def test_markdown_rejection_falls_back_to_text(self):
        self.enable_execution()
        settings = SettingsStore(self.path, self.path.parent / "backup.json")
        config = settings.load_or_migrate({})
        config.update(protected_target_policy="group_message", protected_target_template="hello {sender_id}", protected_target_quote=True)
        settings.save(config)
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        class Session(FakeSession):
            def request(self, method, url, **kwargs):
                if url.endswith("restrict_chat_setting"):
                    self.calls.append((method, url, kwargs))
                    return FakeResponse(400, {"code": 40103004})
                if url.endswith("/messages"):
                    self.calls.append((method, url, kwargs))
                    return FakeResponse(403, {"code": 123}) if kwargs["json"]["msg_type"] == 2 else FakeResponse(200, {"id": "sent"})
                return super().request(method, url, **kwargs)
        session = Session()
        async def sleep(delay): pass
        self.assertEqual(asyncio.run(MuteExecutor(MuteExecutionStore(self.path), QQMutePlatform(session, lambda _: ("app", "secret")), sleep=sleep).execute(1, mode="auto")), "blocked")
        bodies = [call[2]["json"] for call in session.calls if call[1].endswith("/messages")]
        self.assertEqual([body["msg_type"] for body in bodies], [2, 0])
        self.assertEqual(bodies[0]["markdown"], {"content": "hello sender"})
        self.assertEqual(bodies[1]["content"], "hello sender")
        for body in bodies:
            self.assertEqual(body["message_reference"], {"message_id": "m"})
            self.assertEqual(body["msg_id"], "m")
        self.assertGreater(bodies[1]["msg_seq"], bodies[0]["msg_seq"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status, send_method, sent_quoted FROM protected_target_notices").fetchone(), ("succeeded", "text", 1))
            self.assertEqual(db.execute("SELECT send_method FROM protected_target_notice_attempts ORDER BY attempt").fetchall(), [("markdown",), ("text",)])

    def test_permission_denial_does_not_mute_or_retry(self):
        self.enable_execution()
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        session = FakeSession()
        session.permission = "member"
        executor = MuteExecutor(MuteExecutionStore(self.path), QQMutePlatform(session, lambda _: ("app", "secret")))
        self.assertEqual(asyncio.run(executor.execute(1, mode="auto")), "permission_denied")
        self.assertFalse(any(url.endswith("restrict_chat_setting") for _, url, _ in session.calls))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status, attempts, reason_code FROM execution_intents").fetchone(),
                             ("permission_denied", 1, "permission_denied"))

    def test_http_4xx_is_retried(self):
        self.enable_execution()
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        session = FakeSession()
        session.mute_status, session.mute_data = 400, {"code": 123}
        delays = []
        async def sleep(delay):
            delays.append(delay)
        executor = MuteExecutor(MuteExecutionStore(self.path), QQMutePlatform(session, lambda _: ("app", "secret")), sleep=sleep)
        self.assertEqual(asyncio.run(executor.execute(1, mode="auto")), "failed")
        self.assertEqual(sum(url.endswith("restrict_chat_setting") for _, url, _ in session.calls), 3)
        self.assertEqual(delays, [1, 2])

    def test_http_5xx_is_unknown_and_not_retried(self):
        self.enable_execution()
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        session = FakeSession()
        session.mute_status = 500
        executor = MuteExecutor(MuteExecutionStore(self.path), QQMutePlatform(session, lambda _: ("app", "secret")))
        self.assertEqual(asyncio.run(executor.execute(1, mode="auto")), "unknown")
        self.assertEqual(sum(url.endswith("restrict_chat_setting") for _, url, _ in session.calls), 1)

    def test_existing_database_migrates_reason_columns(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("ALTER TABLE execution_intents DROP COLUMN reason_code")
            db.execute("ALTER TABLE execution_attempts DROP COLUMN reason_code")
            db.commit()
        self.queue.initialize()
        self.queue.initialize()
        with closing(sqlite3.connect(self.path)) as db:
            for table in ("execution_intents", "execution_attempts"):
                self.assertIn("reason_code", [row[1] for row in db.execute(f"PRAGMA table_info({table})")])

    def test_cancelled_platform_call_releases_lock_and_records_unknown(self):
        self.enable_execution()
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        async def exercise():
            entered = asyncio.Event()
            class FakePlatform:
                async def verify_permission(self, *args):
                    return True
                async def mute(self, *args):
                    entered.set()
                    await asyncio.Event().wait()
            task = asyncio.create_task(MuteExecutor(MuteExecutionStore(self.path), FakePlatform()).execute(1, mode="auto"))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(exercise())
        with closing(sqlite3.connect(self.path, timeout=1)) as db:
            db.execute("BEGIN IMMEDIATE")
            self.assertEqual(db.execute("SELECT status FROM execution_intents").fetchone()[0], "unknown")
            self.assertEqual(db.execute("SELECT status FROM execution_attempts").fetchone()[0], "unknown")

    def test_recovery_marks_interrupted_attempt_unknown_and_retry_for_review(self):
        self.enable_execution()
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        store = MuteExecutionStore(self.path)
        store.begin_attempt(1, requested_mode="auto")
        store.recover_interrupted()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM execution_intents").fetchone()[0], "unknown")
            self.assertEqual(db.execute("SELECT status FROM execution_attempts").fetchone()[0], "unknown")
            db.execute("UPDATE execution_intents SET status='pending_retry'")
            db.commit()
        store.recover_interrupted()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM execution_intents").fetchone()[0], "review")

    def test_disabled_does_not_contact_platform(self):
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        class FakePlatform:
            async def verify_permission(self, *args):
                raise AssertionError("unexpected platform call")
        result = asyncio.run(MuteExecutor(MuteExecutionStore(self.path), FakePlatform()).execute(1, mode="auto"))
        self.assertEqual(result, "not_claimed")

    def test_rule_change_during_permission_check_blocks_mute(self):
        self.enable_execution()
        self.queue.finish(self.item, status="proposed", detail=self.detail, rule_versions=(("r", 1),))
        rules = self.rules
        class FakePlatform:
            async def verify_permission(self, platform_id, group_id):
                rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=1,
                           text="changed", enabled=False, allowed_actions=["mute"], duration_mode="strict",
                           severity_seconds={"low": 60}, modified_by="admin")
                return True
            async def mute(self, *args):
                raise AssertionError("must not mute after rule change")
        result = asyncio.run(MuteExecutor(MuteExecutionStore(self.path), FakePlatform()).execute(1, mode="auto"))
        self.assertEqual(result, "review")
