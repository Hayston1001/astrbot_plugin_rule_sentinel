import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sentinel.dry_run_queue import DryRunQueue
from sentinel.mute_execution_store import MuteExecutionStore
from sentinel.mute_executor import MuteExecutor
from sentinel.qq_mute_platform import QQMutePlatform
from sentinel.rule_store import RuleStore
from sentinel.settings_store import SettingsStore
from test_qq_mute_platform import FakeSession


class RecallExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "state.sqlite"
        self.rules, self.queue = RuleStore(self.path), DryRunQueue(self.path)
        self.rules.initialize()
        self.queue.initialize()
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=0, text="rule", enabled=True,
                        actions=[{"type": "recall", "mode": "auto"}], modified_by="admin")
        settings = SettingsStore(self.path, self.root / "backup")
        config = settings.load_or_migrate({})
        config.update(action_execution_enabled=True, monitoring_groups=["p/g"],
                      decision_backend="llm", llm_provider_id="model")
        settings.save(config)
        self.settings, self.config = settings, config

    def create_intent(self, age=0, *, include_time=True, mode="auto", version=1):
        payload = {"text": "private", "sender_id": "sender"}
        if include_time:
            payload["message_time"] = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
        self.queue.enqueue(platform_id="p", group_id="g", message_id="trigger/id", payload=payload, capacity=10)
        item = self.queue.claim("p", "g")
        detail = json.dumps({"proposal": {"action": "recall", "rule_id": "r", "rule_version": version,
                             "target_sender_id": "sender", "mode": mode}})
        self.queue.finish(item, status="proposed", detail=detail, rule_versions=(("r", version),))

    def state(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT status, reason_code, attempts FROM execution_intents").fetchone()

    def test_109_seconds_executes_with_trusted_message_id(self):
        self.create_intent(age=109)
        calls = []
        class Platform:
            async def verify_permission(self, *args): return True
            async def recall(self, *args):
                calls.append(args)
                return {"outcome": "succeeded", "platform_code": "200", "trace_id": "trace-1"}
        self.assertEqual(asyncio.run(MuteExecutor(MuteExecutionStore(self.path), Platform()).execute(1, mode="auto")), "succeeded")
        self.assertEqual(calls, [("p", "g", "trigger/id")])
        self.assertEqual(self.state(), ("succeeded", None, 1))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status, platform_code, trace_id FROM execution_attempts").fetchone(), ("succeeded", "200", "trace-1"))

    def test_111_seconds_is_blocked_without_platform_call(self):
        self.create_intent(age=111)
        class Platform:
            async def verify_permission(self, *args): raise AssertionError("platform must not be called")
        asyncio.run(MuteExecutor(MuteExecutionStore(self.path), Platform()).execute(1, mode="auto"))
        self.assertEqual(self.state(), ("blocked", "recall_expired", 0))

    def test_missing_timestamp_requires_review_without_platform_call(self):
        self.create_intent(include_time=False)
        self.assertEqual(self.state(), ("review", "invalid_message_time", 0))

    def test_timeout_never_retries(self):
        self.create_intent()
        calls = []
        class Platform:
            async def verify_permission(self, *args): return True
            async def recall(self, *args):
                calls.append(args)
                raise TimeoutError()
        self.assertEqual(asyncio.run(MuteExecutor(MuteExecutionStore(self.path), Platform()).execute(1, mode="auto")), "unknown")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.state(), ("unknown", None, 1))

    def test_expiry_during_permission_check_prevents_recall(self):
        self.create_intent()
        class Platform:
            async def verify_permission(self, *args): return True
            async def recall(self, *args): raise AssertionError("must not recall")
        with patch.object(MuteExecutionStore, "_recall_expired", side_effect=[False, True, True]):
            self.assertEqual(asyncio.run(MuteExecutor(MuteExecutionStore(self.path), Platform()).execute(1, mode="auto")), "blocked")
        self.assertEqual(self.state(), ("blocked", "recall_expired", 1))

    def test_confirmed_recall_is_idempotent(self):
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=1, text="rule", enabled=True,
                        actions=[{"type": "recall", "mode": "confirm"}], modified_by="admin")
        self.create_intent(mode="confirm", version=2)
        calls = []
        class Platform:
            async def verify_permission(self, *args): return True
            async def recall(self, *args):
                calls.append(args)
                return {"outcome": "succeeded"}
        executor = MuteExecutor(MuteExecutionStore(self.path), Platform())
        async def confirm():
            return await asyncio.gather(executor.execute(1, mode="confirm"), executor.execute(1, mode="confirm"))
        self.assertCountEqual(asyncio.run(confirm()), ["succeeded", "not_claimed"])
        self.assertEqual(calls, [("p", "g", "trigger/id")])

    def test_protected_recall_sends_configured_action_notice(self):
        self.config.update(protected_target_policy="group_message", protected_target_template="{rule_id} {action} {sender_id}")
        self.settings.save(self.config)
        self.create_intent()
        calls = []
        class Platform:
            async def verify_permission(self, *args): return True
            async def recall(self, *args): return {"outcome": "blocked", "reason_code": "protected_target", "platform_code": "40062003"}
            async def send_group_message(self, *args, **kwargs):
                calls.append((args, kwargs))
                return {"outcome": "succeeded"}
        self.assertEqual(asyncio.run(MuteExecutor(MuteExecutionStore(self.path), Platform()).execute(1, mode="auto")), "blocked")
        self.assertEqual(calls[0][0], ("p", "g", "r recall sender"))
        self.assertEqual(self.state(), ("blocked", "protected_target", 1))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM protected_target_notices").fetchone()[0], "succeeded")

    def test_recall_error_outcomes_and_retry_limits(self):
        self.create_intent()
        calls, pauses = [], []
        class Platform:
            async def verify_permission(self, *args): return True
            async def recall(self, *args):
                calls.append(args)
                return {"outcome": "failed", "platform_code": "50065001"}
        async def sleep(delay): pauses.append(delay)
        self.assertEqual(asyncio.run(MuteExecutor(MuteExecutionStore(self.path), Platform(), sleep=sleep).execute(1, mode="auto")), "failed")
        self.assertEqual(len(calls), 3)
        self.assertEqual(pauses, [1, 2])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM execution_attempts").fetchone()[0], 3)

    def test_qq_platform_recall_classification(self):
        async def run():
            session = FakeSession()
            platform = QQMutePlatform(session, lambda _: ("app", "secret"))
            for status, code, expected, reason in ((200, None, "succeeded", None), (400, 40062003, "blocked", "protected_target"),
                (400, 40064004, "blocked", "recall_expired"), (400, 40061001, "failed", "invalid_message"),
                (400, 50065001, "failed", None), (500, 50065001, "unknown", None)):
                session.mute_status = status
                session.mute_data = {"code": code} if code else None
                result = await platform.recall("p", "g", "trigger/id")
                self.assertEqual((result["outcome"], result.get("reason_code")), (expected, reason))
                self.assertEqual(session.calls[-1][0], "DELETE")
                self.assertTrue(session.calls[-1][1].endswith("/v2/groups/g/messages/trigger%2Fid"))
        asyncio.run(run())

    def test_http_recall_failures_are_recorded_without_unsafe_retries(self):
        self.create_intent()
        cases = ((400, 40062003, "blocked", "protected_target", 1),
                 (400, 40064004, "blocked", "recall_expired", 1),
                 (400, 40061001, "failed", "invalid_message", 1),
                 (400, 50065001, "failed", None, 3),
                 (500, 50065001, "unknown", None, 1))
        async def sleep(delay): pass
        for status, code, expected, reason, attempts in cases:
            with self.subTest(code=code, status=status):
                with closing(sqlite3.connect(self.path)) as db:
                    db.execute("DELETE FROM execution_attempts")
                    db.execute("UPDATE execution_intents SET status='pending_auto', attempts=0, reason_code=NULL, platform_code=NULL")
                    db.commit()
                session = FakeSession()
                session.mute_status, session.mute_data = status, {"code": code}
                result = asyncio.run(MuteExecutor(MuteExecutionStore(self.path), QQMutePlatform(session, lambda _: ("app", "secret")), sleep=sleep).execute(1, mode="auto"))
                self.assertEqual(result, expected)
                self.assertEqual(self.state(), (expected, reason, attempts))
                self.assertEqual(sum(method == "DELETE" for method, _, _ in session.calls), attempts)
                with closing(sqlite3.connect(self.path)) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM execution_attempts").fetchone()[0], attempts)

    def test_rule_change_prevents_recall(self):
        self.create_intent()
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=1, text="changed", enabled=False,
                        actions=[{"type": "recall", "mode": "auto"}], modified_by="admin")
        class Platform:
            async def verify_permission(self, *args): raise AssertionError("must not call platform")
        asyncio.run(MuteExecutor(MuteExecutionStore(self.path), Platform()).execute(1, mode="auto"))
        self.assertEqual(self.state()[0], "review")


if __name__ == "__main__":
    unittest.main()
