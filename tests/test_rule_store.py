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

    def test_unchanged_save_keeps_revision_and_original_modifier(self):
        first = self.store.save(**self.args)
        saved = self.store.save(**{**self.args, "expected_version": first.version, "modified_by": "other"})
        self.assertEqual(saved, first)
        self.assertEqual(len(self.store.history("qq", "g1", "r1")), 1)

    def test_stale_unchanged_save_still_conflicts(self):
        self.store.save(**self.args)
        self.store.save(**{**self.args, "expected_version": 1, "text": "Changed"})
        with self.assertRaises(VersionConflict):
            self.store.save(**{**self.args, "expected_version": 1, "text": "Changed"})

    def test_each_config_change_creates_a_record(self):
        cases = [
            {"text": "Changed"},
            {"enabled": False},
            {"priority": 1},
            {"severity_seconds": {"low": 120}},
        ]
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                args = {**self.args, "rule_id": f"case-{index}"}
                self.store.save(**args)
                saved = self.store.save(**{**args, **changes, "expected_version": 1})
                self.assertEqual(saved.version, 2)
                self.assertEqual(len(self.store.history("qq", "g1", args["rule_id"])), 2)

    def test_duration_units_round_trip_and_legacy_actions_need_no_migration(self):
        args = {key: value for key, value in self.args.items()
                if key not in ("allowed_actions", "duration_mode", "severity_seconds")}
        mute = {"type": "mute", "duration_mode": "cumulative",
                "window_seconds": 3600, "tiers_seconds": [60, 60, 3600]}
        old = self.store.save(**args, actions=[mute])
        self.assertEqual(old.actions, [mute])
        new_mute = {**mute, "window_unit": 60, "tier_units": [1, 60, 3600]}
        saved = self.store.save(**{**args, "expected_version": 1}, actions=[new_mute])
        same = self.store.save(**{**args, "expected_version": 2}, actions=[new_mute])
        self.assertEqual(saved, same)
        self.assertEqual([rule.actions for rule in RuleStore(self.path).history("qq", "g1", "r1")],
                         [[mute], [new_mute]])

    def test_invalid_display_units_are_not_saved(self):
        args = {key: value for key, value in self.args.items()
                if key not in ("allowed_actions", "duration_mode", "severity_seconds")}
        cumulative = {"type": "mute", "duration_mode": "cumulative",
                      "window_seconds": 3600, "tiers_seconds": [60, 90]}
        strict = {"type": "mute", "duration_mode": "strict", "severity_seconds": {"low": 90}}
        for action in (cumulative | {"window_unit": True}, cumulative | {"tier_units": [60]},
                       cumulative | {"tier_units": [60, 60]}, strict | {"severity_units": {"low": "60"}},
                       strict | {"severity_units": {"extra": 1}}):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.store.save(**args, actions=[action])
        valid = strict | {"severity_units": {"low": 1}}
        self.assertEqual(self.store.save(**args, actions=[valid]).actions, [valid])

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
            self.assertEqual(item.actions, [{"type": "mute", "duration_mode": "strict", "severity_seconds": {"low": 60}}])
            self.assertEqual(item.execution_mode, "auto")
        with closing(sqlite3.connect(old_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM rule_versions").fetchone()[0], 2)
            self.assertTrue(all("mode" not in json.loads(row[0])[0] for row in db.execute("SELECT actions FROM rule_versions")))
            self.assertEqual([row[0] for row in db.execute("SELECT execution_mode FROM rule_versions")], ["auto", "auto"])

    def test_action_list_roundtrip_and_legacy_read_fallback(self):
        actions = [{"type": "reply", "template": "{sender_id}"}, {"type": "recall"}]
        fields = {key: value for key, value in self.args.items() if key not in ("allowed_actions", "duration_mode", "severity_seconds")}
        saved = self.store.save(**fields, actions=actions, execution_mode="confirm")
        self.assertEqual(list(saved.actions), actions)
        self.assertEqual(saved.execution_mode, "confirm")
        self.assertEqual(list(self.store.current_for_group("qq", "g1")[0].actions), actions)
        self.store.save(**{**self.args, "rule_id": "old"})
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE rule_versions SET actions=NULL WHERE rule_id='old'")
            db.commit()
        self.assertEqual(self.store.history("qq", "g1", "old")[0].actions[0]["type"], "mute")
        self.store.initialize()
        self.store.initialize()
        migrated = self.store.history("qq", "g1", "old")[0]
        self.assertNotIn("mode", migrated.actions[0])
        self.assertEqual(migrated.execution_mode, "auto")

    def test_priority_validation(self):
        with self.assertRaises(ValueError):
            self.store.save(**{**self.args, "priority": True})
        saved = self.store.save(**{**self.args, "priority": 42})
        self.assertEqual(saved.priority, 42)

    def rule_fields(self, **changes):
        fields = {key: value for key, value in self.args.items() if key not in ("allowed_actions", "duration_mode", "severity_seconds")}
        return {**fields, "actions": [{"type": "reply", "template": "notice"}, {"type": "recall"}], **changes}

    def test_execution_mode_is_saved_per_rule_and_defaults(self):
        self.assertEqual(self.store.save(**self.args).execution_mode, "auto")
        self.assertEqual(self.store.save(**self.rule_fields(rule_id="a")).execution_mode, "inherit")
        for mode in ("auto", "confirm", "inherit"):
            with self.subTest(mode=mode):
                saved = self.store.save(**self.rule_fields(rule_id=f"m-{mode}", execution_mode=mode))
                self.assertEqual(saved.execution_mode, mode)
                self.assertEqual(RuleStore(self.path).history("qq", "g1", f"m-{mode}")[0].execution_mode, mode)
                self.assertTrue(all("mode" not in action for action in saved.actions))

    def test_invalid_execution_mode_is_not_saved(self):
        for mode in ("other", "", 1, True):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.store.save(**self.rule_fields(execution_mode=mode))
        self.assertEqual(self.store.history("qq", "g1", "r1"), [])

    def test_changing_only_the_execution_mode_creates_a_version(self):
        first = self.store.save(**self.rule_fields(execution_mode="auto"))
        same = self.store.save(**self.rule_fields(expected_version=1, execution_mode="auto", modified_by="other"))
        self.assertEqual(same, first)
        second = self.store.save(**self.rule_fields(expected_version=1, execution_mode="confirm", modified_by="other"))
        self.assertEqual((second.version, second.execution_mode, second.modified_by), (2, "confirm", "other"))
        self.assertEqual([rule.execution_mode for rule in self.store.history("qq", "g1", "r1")], ["auto", "confirm"])

    def test_legacy_action_modes_are_converted_without_a_new_version(self):
        cases = {"confirm": [{"type": "reply", "mode": "auto", "template": "n"}, {"type": "recall", "mode": "confirm"}],
                 "auto": [{"type": "reply", "mode": "inherit", "template": "n"}, {"type": "recall", "mode": "auto"}],
                 "inherit": [{"type": "reply", "mode": "inherit", "template": "n"}, {"type": "recall", "mode": "inherit"}]}
        for expected, actions in cases.items():
            with self.subTest(expected=expected):
                saved = self.store.save(**self.rule_fields(rule_id=expected, actions=actions))
                self.assertEqual(saved.execution_mode, expected)
                self.assertTrue(all("mode" not in action for action in saved.actions))
                again = self.store.save(**self.rule_fields(rule_id=expected, actions=actions, expected_version=1))
                self.assertEqual(again, saved)
                self.assertEqual(len(self.store.history("qq", "g1", expected)), 1)

    def test_initialize_backfills_rule_mode_from_stored_legacy_action_modes(self):
        old_path = self.path.parent / "modes.sqlite"
        cases = {"mixed": ([{"type": "reply", "mode": "auto", "template": "n"}, {"type": "recall", "mode": "confirm"}], "confirm"),
                 "auto": ([{"type": "reply", "mode": "inherit", "template": "n"}, {"type": "recall", "mode": "auto"}], "auto"),
                 "follow": ([{"type": "reply", "mode": "inherit", "template": "n"}, {"type": "recall", "mode": "inherit"}], "inherit")}
        with closing(sqlite3.connect(old_path)) as db:
            db.execute("""CREATE TABLE rule_versions (
                platform_id TEXT NOT NULL, group_id TEXT NOT NULL, rule_id TEXT NOT NULL,
                version INTEGER NOT NULL, text TEXT NOT NULL, enabled INTEGER NOT NULL,
                allowed_actions TEXT NOT NULL, duration_mode TEXT NOT NULL,
                severity_seconds TEXT NOT NULL, modified_by TEXT NOT NULL, created_at TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0, actions TEXT,
                PRIMARY KEY (platform_id, group_id, rule_id, version))""")
            for rule_id, (actions, _) in cases.items():
                db.execute("INSERT INTO rule_versions VALUES ('qq', 'g', ?, 1, 'rule', 1, ?, 'strict', '{}', 'admin', 'time', 0, ?)",
                           (rule_id, json.dumps([item["type"] for item in actions]), json.dumps(actions)))
            db.commit()
            before = db.execute("SELECT rule_id, actions FROM rule_versions ORDER BY rule_id").fetchall()
        store = RuleStore(old_path)
        store.initialize()
        store.initialize()
        for rule_id, (_, expected) in cases.items():
            history = store.history("qq", "g", rule_id)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].execution_mode, expected)
            self.assertTrue(all("mode" not in action for action in history[0].actions))
        with closing(sqlite3.connect(old_path)) as db:
            self.assertEqual(db.execute("SELECT rule_id, actions FROM rule_versions ORDER BY rule_id").fetchall(), before)

    def test_legacy_allowed_actions_rows_become_automatic_and_disabled_rows_follow_global(self):
        self.store.save(**self.args)
        self.store.save(**{**self.args, "rule_id": "none", "allowed_actions": [], "enabled": False})
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE rule_versions SET actions=NULL, execution_mode=NULL")
            db.commit()
        self.store.initialize()
        self.assertEqual(self.store.history("qq", "g1", "r1")[0].execution_mode, "auto")
        self.assertEqual(self.store.history("qq", "g1", "none")[0].execution_mode, "inherit")

    def test_corrupt_stored_execution_mode_is_rejected(self):
        self.store.save(**self.args)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE rule_versions SET execution_mode='bogus'")
            db.commit()
        with self.assertRaises(ValueError):
            self.store.history("qq", "g1", "r1")


if __name__ == "__main__":
    unittest.main()
