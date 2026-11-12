import tempfile
import unittest
import sqlite3
import json
from contextlib import closing
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from sentinel.rule_store import RuleStore, VersionConflict


class RuleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "rules.sqlite"
        self.store = RuleStore(self.path)
        self.store.initialize()
        self.args = dict(platform_id="qq", group_id="g1", rule_id="r1", expected_version=0,
                         text="No spam", enabled=True, allowed_actions=["mute"],
                         duration_mode="strict", severity_seconds={"low": 60}, modified_by="admin")

    def test_versions_survive_reopen_and_keep_history(self):
        first = self.store.save(**self.args)
        second = self.store.save(**{**self.args, "expected_version": 1, "enabled": False, "modified_by": "admin2"})
        history = RuleStore(self.path).history("qq", "g1", "r1")
        self.assertEqual([entry.version for entry in history], [1, 2])
        self.assertTrue(first.enabled)
        self.assertFalse(second.enabled)
        self.assertEqual(history[0].severity_seconds, {"low": 60})
        self.assertEqual(history[1].modified_by, "admin2")

    def test_version_conflict_does_not_write(self):
        self.store.save(**self.args)
        with self.assertRaises(VersionConflict):
            self.store.save(**self.args)
        self.assertEqual(len(self.store.history("qq", "g1", "r1")), 1)

    def test_group_and_platform_are_isolated(self):
        self.store.save(**self.args)
        self.store.save(**{**self.args, "group_id": "g2"})
        self.assertEqual(self.store.history("qq", "g3", "r1"), [])
        self.assertEqual(self.store.history("other", "g1", "r1"), [])
        self.assertEqual(len(self.store.history("qq", "g2", "r1")), 1)

    def test_invalid_configuration_is_not_saved(self):
        for changes in ({"severity_seconds": {}}, {"severity_seconds": {"low": True}},
                        {"enabled": 1}, {"allowed_actions": ["unknown"]},
                        {"duration_mode": "autonomous"}, {"modified_by": " "}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.save(**{**self.args, **changes})
        self.assertEqual(self.store.history("qq", "g1", "r1"), [])

    def test_autonomous_policy(self):
        result = self.store.save(**{**self.args, "duration_mode": "autonomous", "severity_seconds": {}})
        self.assertEqual(result.duration_mode, "autonomous")

    def test_current_only_returns_latest_versions_in_requested_group(self):
        self.store.save(**self.args)
        self.store.save(**{**self.args, "expected_version": 1, "enabled": False})
        self.store.save(**{**self.args, "rule_id": "r2"})
        self.store.save(**{**self.args, "group_id": "other"})
        current = self.store.current_for_group("qq", "g1")
        self.assertEqual([(rule.rule_id, rule.version, rule.enabled) for rule in current], [("r1", 2, False), ("r2", 1, True)])

    def test_concurrent_edit_with_same_version_only_one_succeeds(self):
        self.store.save(**self.args)
        def save(text):
            try:
                self.store.save(**{**self.args, "expected_version": 1, "text": text})
                return "saved"
            except VersionConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ("edit one", "edit two")))
        self.assertCountEqual(results, ["saved", "conflict"])
        self.assertEqual([rule.version for rule in self.store.history("qq", "g1", "r1")], [1, 2])

    def test_corrupt_enabled_value_is_not_treated_as_enabled(self):
        self.store.save(**self.args)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE rule_versions SET enabled=2")
            db.commit()
        with self.assertRaises(ValueError):
            self.store.current_for_group("qq", "g1")

    def test_legacy_rules_gain_priority_without_losing_history(self):
        old_path = self.path.parent / "old.sqlite"
        with closing(sqlite3.connect(old_path)) as db:
            db.execute("""CREATE TABLE rule_versions (
                platform_id TEXT NOT NULL, group_id TEXT NOT NULL, rule_id TEXT NOT NULL,
                version INTEGER NOT NULL, text TEXT NOT NULL, enabled INTEGER NOT NULL,
                allowed_actions TEXT NOT NULL, duration_mode TEXT NOT NULL,
                severity_seconds TEXT NOT NULL, modified_by TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (platform_id, group_id, rule_id, version))""")
            db.execute("INSERT INTO rule_versions VALUES ('qq', 'g', 'r', 1, 'rule', 1, '[\"mute\"]', 'strict', '{\"low\":60}', 'admin', 'time')")
            db.execute("INSERT INTO rule_versions VALUES ('qq', 'g', 'r', 2, 'rule', 0, '[\"mute\"]', 'strict', '{\"low\":60}', 'admin', 'time')")
            db.commit()
        store = RuleStore(old_path)
        store.initialize()
        store.initialize()
        history = store.history("qq", "g", "r")
        self.assertEqual((history[0].version, history[0].priority), (1, 0))
        self.assertFalse(history[1].enabled)
        for item in history:
            self.assertEqual(item.actions, [{"type": "mute", "mode": "auto", "duration_mode": "strict", "severity_seconds": {"low": 60}}])
        with closing(sqlite3.connect(old_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM rule_versions").fetchone()[0], 2)
            self.assertTrue(all(json.loads(row[0])[0]["mode"] == "auto" for row in db.execute("SELECT actions FROM rule_versions")))

    def test_action_list_roundtrip_and_legacy_read_fallback(self):
        actions = [{"type": "reply", "mode": "confirm", "template": "{sender_id}"}, {"type": "recall", "mode": "auto"}]
        fields = {key: value for key, value in self.args.items() if key not in ("allowed_actions", "duration_mode", "severity_seconds")}
        saved = self.store.save(**fields, actions=actions)
        self.assertEqual(list(saved.actions), actions)
        self.assertEqual(list(self.store.current_for_group("qq", "g1")[0].actions), actions)
        self.store.save(**{**self.args, "rule_id": "old"})
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE rule_versions SET actions=NULL WHERE rule_id='old'")
            db.commit()
        self.assertEqual(self.store.history("qq", "g1", "old")[0].actions[0]["type"], "mute")
        self.store.initialize()
        self.store.initialize()
        self.assertEqual(self.store.history("qq", "g1", "old")[0].actions[0]["mode"], "auto")

    def test_priority_validation(self):
        with self.assertRaises(ValueError):
            self.store.save(**{**self.args, "priority": True})
        saved = self.store.save(**{**self.args, "priority": 42})
        self.assertEqual(saved.priority, 42)


if __name__ == "__main__":
    unittest.main()
