import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sentinel.dry_run_ingest import DryRunAdmission, IncomingMessage, MAX_MESSAGE_CHARS
from sentinel.dry_run_queue import DryRunQueue
from sentinel.monitoring_policy import MonitoringPolicy
from sentinel.rule_store import RuleStore
from sentinel.event_intake import Skip
from sentinel.framework_session_gate import FrameworkSessionGate


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "state.sqlite"
        self.queue = DryRunQueue(self.path)
        self.queue.initialize()
        self.rules = RuleStore(self.path)
        self.rules.initialize()
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                        text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                        severity_seconds={"low": 10}, modified_by="admin")
        self.policy = MonitoringPolicy("allowlist", frozenset({("p", "g")}))
        self.message = IncomingMessage("p", "g", "m", "u", "bot", "private", True, False)

    async def test_enabled_group_is_queued_once(self):
        admission = DryRunAdmission(self.queue, self.rules, policy=self.policy, capacity=1, model="m")
        self.assertEqual(await admission.submit(self.message), "queued")
        self.assertEqual(await admission.submit(self.message), "duplicate")
        item = self.queue.claim("p", "g")
        self.assertEqual(item.payload, {"text": "private", "sender_id": "u", "session_origin": None})

    async def test_non_text_skip_does_not_record_message(self):
        admission = DryRunAdmission(self.queue, self.rules, policy=self.policy, capacity=1, model="m")
        event = IncomingMessage("p", "g", "m", "u", "bot", "private", False, False)
        self.assertEqual(await admission.submit(event), "non_text_or_unknown")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status, detail FROM dry_run_events").fetchone(), ("skipped", "non_text_or_unknown"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dry_run_queue").fetchone()[0], 0)

    async def test_disallowed_group_and_unconfigured_model(self):
        blocked = MonitoringPolicy("allowlist", frozenset())
        admission = DryRunAdmission(self.queue, self.rules, policy=blocked, capacity=100, model="m")
        self.assertEqual(await admission.submit(self.message), "group_not_allowed")
        with self.assertRaises(ValueError):
            DryRunAdmission(self.queue, self.rules, policy=self.policy, capacity=100, model="")

    async def test_oversized_message_is_skipped_without_truncation(self):
        admission = DryRunAdmission(self.queue, self.rules, policy=self.policy, capacity=100, model="m")
        event = IncomingMessage("p", "g", "m", "u", "bot", "x" * (MAX_MESSAGE_CHARS + 1), True, False)
        self.assertEqual(await admission.submit(event), "message_too_long")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT detail FROM dry_run_events").fetchone()[0], "message_too_long")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dry_run_queue").fetchone()[0], 0)

    async def test_denied_framework_session_does_not_store_text(self):
        async def deny(message):
            return False
        admission = DryRunAdmission(self.queue, self.rules, policy=self.policy, capacity=100, model="m", session_gate=deny)
        self.assertEqual(await admission.submit(self.message), "framework_session_denied")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dry_run_queue").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dry_run_events").fetchone()[0], 0)

    async def test_capture_skip_requires_framework_authorization(self):
        permission = [False]
        async def gate(message):
            return permission[0]
        admission = DryRunAdmission(self.queue, self.rules, policy=self.policy, capacity=100, model="m", session_gate=gate)
        skipped = Skip("p", "g", "id", "bot_identity_unverified", "p:GroupMessage:g")
        self.assertEqual(await admission.record_external_skip(skipped), "framework_session_denied")
        permission[0] = True
        self.assertEqual(await admission.record_external_skip(skipped), "skipped")
        self.assertEqual(self.queue.list_events(platform_id="p", group_id="g")[0]["detail"], "bot_identity_unverified")

    async def test_mismatched_framework_session_never_persists_message(self):
        async def allow(*args):
            return True
        gate = FrameworkSessionGate(get_config=lambda origin: {"platform_settings": {
            "enable_id_white_list": False, "id_whitelist": []}},
            is_session_enabled=allow, is_plugin_enabled=allow, plugin_name="plugin")
        admission = DryRunAdmission(self.queue, self.rules, policy=self.policy, capacity=100, model="m", session_gate=gate)
        event = IncomingMessage("p", "g", "m", "u", "bot", "private", True, False,
                                session_origin="p:GroupMessage:other")
        self.assertEqual(await admission.submit(event), "framework_session_denied")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dry_run_queue").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dry_run_events").fetchone()[0], 0)
