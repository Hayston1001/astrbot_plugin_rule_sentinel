import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone

from sentinel.action_config import resolve_action_mode, validate_actions
from sentinel.dry_run_queue import DryRunQueue
from sentinel.group_settings import decision_fingerprint
from sentinel.mute_execution_store import MuteExecutionStore
from sentinel.rule_management import RuleManagement
from sentinel.rule_store import RuleStore, VersionConflict
from sentinel.settings_store import SettingsStore, validate_settings


class ActionDefaultTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.sqlite"
        self.rules, self.queue = RuleStore(self.path), DryRunQueue(self.path)
        self.rules.initialize()
        self.queue.initialize()
        self.settings = SettingsStore(self.path, Path(directory.name) / "backup")
        self.config = self.settings.load_or_migrate({})
        self.config.update(monitoring_groups=["p/g"],
                           action_execution_enabled=True, decision_backend="llm", llm_provider_id="model")
        self.settings.save(self.config)
        self.fields = {"platform_id": "p", "group_id": "g", "rule_id": "r", "expected_version": 0,
                       "text": "rule", "enabled": True, "actions": [
                           {"type": "reply", "mode": "inherit", "template": "notice"},
                           {"type": "mute", "mode": "inherit", "duration_mode": "strict", "severity_seconds": {"low": 60}},
                           {"type": "recall", "mode": "confirm"}]}

    def test_mode_validation_and_migration(self):
        self.assertEqual(resolve_action_mode("inherit", "confirm"), "confirm")
        self.assertEqual(resolve_action_mode("auto", "confirm"), "auto")
        for value in (None, "invalid", 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_action_mode("inherit", value)
        self.assertEqual(validate_actions(self.fields["actions"]), self.fields["actions"])
        with self.assertRaises(ValueError):
            validate_settings({**self.config, "default_action_mode": "invalid"})
        old = copy.deepcopy(self.config)
        old.pop("default_action_mode")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE dashboard_settings SET payload=?", (json.dumps(old),))
            db.commit()
        self.assertEqual(self.settings.load_or_migrate({})["default_action_mode"], "auto")

    def test_preview_binds_global_value_until_confirmation(self):
        manager = RuleManagement(self.rules, lambda *_: True, confirmation_seconds=60)
        preview = manager.preview("admin", self.fields)
        self.assertEqual(preview["default_action_mode"], "auto")
        with self.assertRaises(ValueError):
            manager.confirm("admin", {**preview, "default_action_mode": "confirm"})
        self.config["default_action_mode"] = "confirm"
        self.settings.save(self.config)
        with self.assertRaises(VersionConflict):
            manager.confirm("admin", preview)
        self.assertEqual(self.rules.history("p", "g", "r"), [])
        fresh = manager.preview("admin", self.fields)
        self.assertEqual(fresh["default_action_mode"], "confirm")
        saved = manager.confirm("admin", fresh)
        self.assertEqual([item["mode"] for item in saved.actions], ["inherit", "inherit", "confirm"])

    def record_plan(self):
        self.rules.save(**self.fields, modified_by="admin")
        self.queue.enqueue(platform_id="p", group_id="g", message_id="m", capacity=10,
                           payload={"sender_id": "sender", "text": "message",
                                    "message_time": datetime.now(timezone.utc).isoformat()})
        item = self.queue.claim("p", "g")
        detail = json.dumps({"proposal": {"rule_id": "r", "rule_version": 1,
                                           "target_sender_id": "sender", "duration_seconds": 60}})
        return item, detail

    def test_inherited_modes_resolve_for_each_action_and_recheck(self):
        self.config["default_action_mode"] = "confirm"
        self.settings.save(self.config)
        item, detail = self.record_plan()
        self.queue.finish(item, status="proposed", detail=detail, rule_versions=(("r", 1),),
                          settings_fingerprint=decision_fingerprint(self.config, "p", "g"))
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT action, mode, status FROM execution_intents ORDER BY action_index").fetchall()
        self.assertEqual(rows, [("reply", "confirm", "pending_confirm"),
                                ("mute", "confirm", "pending_confirm"),
                                ("recall", "confirm", "pending_confirm")])
        store = MuteExecutionStore(self.path)
        self.assertIsNone(store.begin_attempt(1, requested_mode="auto"))
        self.assertIsNotNone(store.begin_attempt(1, requested_mode="confirm"))
        self.config["default_action_mode"] = "auto"
        self.settings.save(self.config)
        self.assertFalse(store.recheck_attempt(1, 1))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM execution_intents WHERE id=2").fetchone()[0], "inactive")

    def test_global_change_during_decision_requires_review(self):
        item, detail = self.record_plan()
        original = decision_fingerprint(self.config, "p", "g")
        self.config["default_action_mode"] = "confirm"
        self.settings.save(self.config)
        self.queue.finish(item, status="proposed", detail=detail, rule_versions=(("r", 1),),
                          settings_fingerprint=original)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM dry_run_events ORDER BY id DESC LIMIT 1").fetchone()[0], "review")
            self.assertEqual(db.execute("SELECT count(*) FROM execution_intents").fetchone()[0], 0)

    def test_missing_global_value_never_creates_inherited_intent(self):
        item, detail = self.record_plan()
        broken = copy.deepcopy(self.config)
        broken.pop("default_action_mode")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE dashboard_settings SET payload=?", (json.dumps(broken),))
            db.commit()
        with self.assertRaises(ValueError):
            self.queue.finish(item, status="proposed", detail=detail, rule_versions=(("r", 1),))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM execution_intents").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM dry_run_queue").fetchone()[0], "processing")
