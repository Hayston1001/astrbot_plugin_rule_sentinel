import json
import importlib.util
import os
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from sentinel.plugin_settings import read_settings
from sentinel.settings_store import DEFAULTS, SettingsStore, capture_legacy_config, validate_settings


class SettingsStoreTests(unittest.TestCase):
    def test_new_install_requires_a_model_to_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root / "state.sqlite", root / "backup.json")
            config = store.load_or_migrate({})
            self.assertNotIn("monitoring_enabled", config)
            self.assertTrue(config["action_execution_enabled"])
            self.assertEqual(config["decision_backend"], "llm")
            self.assertEqual(config["llm_provider_id"], "")
            for key, expected in {"max_duration_seconds": 2592000, "model_concurrency": 20,
                                  "model_timeout_seconds": 35, "global_rate_per_minute": 100,
                                  "group_rate_per_minute": 60, "model_failure_threshold": 5,
                                  "model_cooldown_seconds": 120, "queue_capacity_per_group": 100}.items():
                with self.subTest(key=key):
                    self.assertEqual(config[key], expected)
            validate_settings(config)
            with self.assertRaises(ValueError):
                read_settings(config)
            store.save({**config, "message_retention_days": 10})
            self.assertEqual(store.load_or_migrate({})["message_retention_days"], 10)
            for changes in ({"model_concurrency": 0}, {"monitoring_enabled": False},
                            {"llm_provider_id": None}, {"decision_backend": "jev"},
                            {"decision_backend": "unknown"}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    store.save({**config, **changes})

    def test_removed_switch_migrates_without_changing_execution(self):
        for monitoring in (False, True):
            for execution in (False, True):
                with self.subTest(monitoring=monitoring, execution=execution), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    store = SettingsStore(root / "state.sqlite", root / "backup.json")
                    original = {**store.load_or_migrate({}), "action_execution_enabled": execution}
                    store.save(original)
                    with closing(sqlite3.connect(store.path)) as db:
                        db.execute("UPDATE dashboard_settings SET payload=? WHERE id=1",
                                   (json.dumps({**original, "monitoring_enabled": monitoring}),))
                        db.commit()
                    self.assertEqual(store.load_or_migrate({}), original)
                    with closing(sqlite3.connect(store.path)) as db:
                        persisted = json.loads(db.execute("SELECT payload FROM dashboard_settings").fetchone()[0])
                    self.assertNotIn("monitoring_enabled", persisted)

    def test_removing_disabled_switch_retires_only_pending_work(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = SettingsStore(root / "state.sqlite", root / "backup.json")
                original = {**store.load_or_migrate({}), "llm_provider_id": "model",
                            "monitoring_groups": ["p/g"]}
                with closing(sqlite3.connect(store.path)) as db:
                    db.execute("UPDATE dashboard_settings SET payload=? WHERE id=1",
                               (json.dumps({**original, "monitoring_enabled": enabled}),))
                    db.execute("CREATE TABLE execution_intents (status TEXT, updated_at TEXT)")
                    db.execute("CREATE TABLE protected_target_notices (status TEXT, updated_at TEXT)")
                    for state in ("pending_auto", "pending_confirm", "pending_retry", "executing", "succeeded"):
                        db.execute("INSERT INTO execution_intents VALUES (?, '')", (state,))
                    for state in ("pending", "pending_retry", "sending", "succeeded"):
                        db.execute("INSERT INTO protected_target_notices VALUES (?, '')", (state,))
                    db.commit()
                self.assertEqual(store.load_or_migrate({}), original)
                with closing(sqlite3.connect(store.path)) as db:
                    intents = [row[0] for row in db.execute("SELECT status FROM execution_intents ORDER BY rowid")]
                    notices = [row[0] for row in db.execute("SELECT status FROM protected_target_notices ORDER BY rowid")]
                self.assertEqual(intents, (["pending_auto", "pending_confirm", "pending_retry"] if enabled
                                           else ["inactive", "inactive", "inactive"]) + ["executing", "succeeded"])
                self.assertEqual(notices, (["pending", "pending_retry"] if enabled else ["inactive", "inactive"])
                                 + ["sending", "succeeded"])

    def test_invalid_legacy_switch_or_unknown_fields_roll_back(self):
        for changes in ({"monitoring_enabled": 1}, {"monitoring_enabled": False, "unexpected": True}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = SettingsStore(root / "state.sqlite", root / "backup.json")
                old = {**store.load_or_migrate({}), **changes}
                with closing(sqlite3.connect(store.path)) as db:
                    db.execute("UPDATE dashboard_settings SET payload=?", (json.dumps(old),))
                    db.commit()
                with self.assertRaises(ValueError):
                    store.load_or_migrate({})
                with closing(sqlite3.connect(store.path)) as db:
                    self.assertEqual(json.loads(db.execute("SELECT payload FROM dashboard_settings").fetchone()[0]), old)

    def test_failed_retirement_rolls_back_actions_and_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root / "state.sqlite", root / "backup.json")
            old = {**store.load_or_migrate({}), "monitoring_enabled": False}
            with closing(sqlite3.connect(store.path)) as db:
                db.execute("UPDATE dashboard_settings SET payload=?", (json.dumps(old),))
                db.execute("CREATE TABLE execution_intents (status TEXT, updated_at TEXT)")
                db.execute("CREATE TABLE protected_target_notices (status TEXT, updated_at TEXT)")
                db.execute("INSERT INTO execution_intents VALUES ('pending_auto', '')")
                db.execute("INSERT INTO protected_target_notices VALUES ('pending', '')")
                db.execute("""CREATE TRIGGER reject_notice_update BEFORE UPDATE ON protected_target_notices
                           BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
                db.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                store.load_or_migrate({})
            with closing(sqlite3.connect(store.path)) as db:
                self.assertEqual(db.execute("SELECT status FROM execution_intents").fetchone()[0], "pending_auto")
                self.assertEqual(db.execute("SELECT status FROM protected_target_notices").fetchone()[0], "pending")
                self.assertEqual(json.loads(db.execute("SELECT payload FROM dashboard_settings").fetchone()[0]), old)

    def test_legacy_file_with_only_old_switch_is_not_a_new_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "config.json"
            source.write_text('{"monitoring_enabled": false}', encoding="utf-8")
            capture_legacy_config(root / "data", source)
            config = SettingsStore(root / "state.sqlite", root / "data" / "legacy-settings.json").load_or_migrate({})
            self.assertNotIn("monitoring_enabled", config)
            self.assertFalse(config["action_execution_enabled"])
            self.assertEqual(config["decision_backend"], "")

    def test_legacy_config_discards_monitoring_switch_and_never_enables_execution(self):
        for legacy in ({"llm_provider_id": "old"},
                       {"monitoring_enabled": False, "action_execution_enabled": True},
                       {"monitoring_enabled": True, "action_execution_enabled": True}):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = SettingsStore(root / "state.sqlite", root / "backup.json")
                config = store.load_or_migrate(legacy)
                self.assertNotIn("monitoring_enabled", config)
                self.assertFalse(config["action_execution_enabled"])
                self.assertEqual(store.load_or_migrate({}), config)

    def test_legacy_migration_preserves_old_limit_defaults_and_explicit_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root / "state.sqlite", root / "backup.json")
            config = store.load_or_migrate({"model_concurrency": 7, "group_rate_per_minute": 15})
            self.assertEqual(config["decision_backend"], "")
            for key, expected in {"model_concurrency": 7, "model_timeout_seconds": 30,
                                  "global_rate_per_minute": 60, "group_rate_per_minute": 15,
                                  "model_cooldown_seconds": 60}.items():
                with self.subTest(key=key):
                    self.assertEqual(config[key], expected)
            self.assertEqual(store.load_or_migrate({}), config)

    def test_existing_saved_limits_do_not_change(self):
        for concurrency in (2, 5):
            with self.subTest(concurrency=concurrency), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = SettingsStore(root / "state.sqlite", root / "backup.json")
                original = {**store.load_or_migrate({}), "model_concurrency": concurrency,
                            "model_timeout_seconds": 30, "global_rate_per_minute": 60,
                            "group_rate_per_minute": 30, "model_cooldown_seconds": 60}
                store.save(original)
                self.assertEqual(store.load_or_migrate({}), original)

    def test_empty_legacy_backup_is_not_mistaken_for_a_new_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "backup.json"
            backup.write_text("{}", encoding="utf-8")
            config = SettingsStore(root / "state.sqlite", backup).load_or_migrate({})
            self.assertNotIn("monitoring_enabled", config)
            self.assertFalse(config["action_execution_enabled"])
            self.assertEqual(config["model_concurrency"], 2)

    def test_reason_only_change_does_not_trigger_unrelated_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "state.sqlite", Path(directory) / "backup")
            config = store.load_or_migrate({})
            with patch("sentinel.message_retention.purge_expired_contents") as purge:
                changed = {**config, "llm_no_action_reason_enabled": False}
                store.save(changed)
                purge.assert_not_called()
                self.assertFalse(store.load_or_migrate({})["llm_no_action_reason_enabled"])
                store.save({**changed, "message_retention_days": 10})
                purge.assert_called_once()

    def test_no_action_reason_switch_migrates_on_and_requires_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "state.sqlite", Path(directory) / "backup")
            original = store.load_or_migrate({})
            self.assertTrue(original["llm_no_action_reason_enabled"])
            original.pop("llm_no_action_reason_enabled")
            with closing(sqlite3.connect(store.path)) as db:
                db.execute("UPDATE dashboard_settings SET payload=? WHERE id=1", (json.dumps(original),))
                db.commit()
            migrated = store.load_or_migrate({})
            self.assertTrue(migrated["llm_no_action_reason_enabled"])
            migrated["llm_no_action_reason_enabled"] = False
            store.save(migrated)
            self.assertFalse(store.load_or_migrate({})["llm_no_action_reason_enabled"])
            for invalid in (None, 0, 1, "false", []):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    store.save({**migrated, "llm_no_action_reason_enabled": invalid})
            self.assertFalse(store.load_or_migrate({})["llm_no_action_reason_enabled"])

    def test_retired_model_migration_disables_affected_groups_and_work(self):
        from sentinel.dry_run_queue import DryRunQueue
        for mode in ("allowlist", "blocklist"):
            for global_retired in (False, True):
                with self.subTest(mode=mode, global_retired=global_retired), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    store = SettingsStore(root / "db.sqlite", root / "backup")
                    queue = DryRunQueue(store.path)
                    queue.initialize()
                    old = store.load_or_migrate({})
                    old.update(decision_backend="jev" if global_retired else "llm", llm_provider_id="previous-llm",
                               jev_provider="typesafe", jev_model="old-model", jev_api_key="old-secret",
                               monitoring_mode=mode, monitoring_groups=["p/old", "p/other"] if mode == "allowlist" else [],
                               group_overrides={"p/old": {"model": {"backend": "jev", "model": "old-model"},
                                                            "strategy": "priority"}})
                    for group in ("old", "other"):
                        queue.enqueue(platform_id="p", group_id=group, message_id="m", capacity=10,
                                      payload={"text": "private", "sender_id": "sender"})
                    queue.claim("p", "old")
                    with closing(sqlite3.connect(store.path)) as db:
                        db.execute("UPDATE dashboard_settings SET payload=? WHERE id=1", (json.dumps(old),))
                        db.commit()
                    migrated = store.load_or_migrate({})
                    validate_settings(migrated)
                    self.assertEqual(migrated["llm_provider_id"], "" if global_retired else "previous-llm")
                    self.assertEqual(migrated["group_overrides"], {"p/old": {"strategy": "priority"}})
                    self.assertEqual("p/old" in migrated["monitoring_groups"], mode == "blocklist")
                    self.assertTrue(all(key not in migrated for key in ("jev_provider", "jev_model", "jev_api_key")))
                    with closing(sqlite3.connect(store.path)) as db:
                        persisted = db.execute("SELECT payload FROM dashboard_settings").fetchone()[0]
                        states = db.execute("SELECT group_id, state, payload, lease FROM dry_run_queue ORDER BY group_id").fetchall()
                        events = db.execute("SELECT group_id, status, detail FROM dry_run_events ORDER BY group_id").fetchall()
                    self.assertNotIn("old-secret", persisted)
                    self.assertEqual(states[0], ("old", "done", None, None))
                    self.assertEqual(states[1][1], "done" if global_retired else "pending")
                    self.assertEqual(events, [("old", "review", "decision_model_removed")]
                                     + ([("other", "review", "decision_model_removed")] if global_retired else []))
                    self.assertEqual(store.load_or_migrate({}), migrated)

    def test_retired_model_migration_retires_only_affected_actions_and_rolls_back_on_error(self):
        from sentinel.dry_run_queue import DryRunQueue
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root / "db.sqlite", root / "backup")
            DryRunQueue(store.path).initialize()
            old = store.load_or_migrate({})
            old.update(llm_provider_id="model", monitoring_groups=["p/old", "p/other"],
                       jev_api_key="secret", group_overrides={"p/old": {"model": {"backend": "jev", "model": "old"}}})
            with closing(sqlite3.connect(store.path)) as db:
                db.execute("UPDATE dashboard_settings SET payload=?", (json.dumps(old),))
                for index, group in enumerate(("old", "other"), 1):
                    db.execute("""INSERT INTO execution_intents (source_event_id, source_attempt_id, platform_id, group_id,
                        message_id, rule_id, rule_version, action, target_sender_id, mode, status, created_at, updated_at,
                        plan_id, action_index) VALUES (?, ?, 'p', ?, 'm', 'r', 1, 'mute', 's', 'auto', 'pending_auto', '', '', ?, 0)""",
                               (index, str(index), group, str(index)))
                    db.execute("INSERT INTO protected_target_notices (intent_id, status, template, quote, created_at, updated_at) "
                               "VALUES (?, 'pending', '', 0, '', '')", (index,))
                db.execute("CREATE TRIGGER reject_retirement BEFORE UPDATE ON execution_intents "
                           "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
                db.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                store.load_or_migrate({})
            with closing(sqlite3.connect(store.path)) as db:
                self.assertEqual(json.loads(db.execute("SELECT payload FROM dashboard_settings").fetchone()[0]), old)
                self.assertEqual(db.execute("SELECT status FROM protected_target_notices ORDER BY intent_id").fetchall(),
                                 [("pending",), ("pending",)])
                db.execute("DROP TRIGGER reject_retirement")
                db.commit()
            store.load_or_migrate({})
            with closing(sqlite3.connect(store.path)) as db:
                self.assertEqual(db.execute("SELECT status FROM execution_intents ORDER BY id").fetchall(),
                                 [("inactive",), ("pending_auto",)])
                self.assertEqual(db.execute("SELECT status FROM protected_target_notices ORDER BY intent_id").fetchall(),
                                 [("inactive",), ("pending",)])

    def test_unused_retired_fields_are_removed_without_changing_llm_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "db.sqlite", Path(directory) / "backup")
            old = store.load_or_migrate({})
            old.update(llm_provider_id="model", jev_model="old", jev_api_key="secret", jev_provider="typesafe")
            with closing(sqlite3.connect(store.path)) as db:
                db.execute("UPDATE dashboard_settings SET payload=?", (json.dumps(old),))
                db.commit()
            migrated = store.load_or_migrate({})
            self.assertEqual(migrated["llm_provider_id"], "model")
            self.assertNotIn("jev_api_key", migrated)

    def test_retired_model_group_remains_discoverable_and_invalid_data_is_not_discarded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "db.sqlite", Path(directory) / "backup")
            old = store.load_or_migrate({})
            old.update(llm_provider_id="model", monitoring_groups=["p/g"], jev_api_key="secret",
                       group_overrides={"p/g": {"model": {"backend": "jev", "model": "old"}}})
            with closing(sqlite3.connect(store.path)) as db:
                db.execute("UPDATE dashboard_settings SET payload=?", (json.dumps(old),))
                db.commit()
            migrated = store.load_or_migrate({})
            self.assertEqual(migrated["group_overrides"], {"p/g": {}})
            self.assertNotIn("p/g", migrated["monitoring_groups"])
            validate_settings(migrated)
            old["group_overrides"]["p/g"]["model"]["unexpected"] = True
            with closing(sqlite3.connect(store.path)) as db:
                db.execute("UPDATE dashboard_settings SET payload=?", (json.dumps(old),))
                db.commit()
            with self.assertRaises(ValueError):
                store.load_or_migrate({})
            with closing(sqlite3.connect(store.path)) as db:
                self.assertEqual(json.loads(db.execute("SELECT payload FROM dashboard_settings").fetchone()[0]), old)
    def test_protected_target_settings_validation_and_migration(self):
        valid = {**DEFAULTS, "protected_target_policy": "group_message",
                 "protected_target_template": "{sender_id} {rule_id}", "protected_target_quote": True}
        validate_settings(valid)
        for changes in ({"protected_target_template": " "}, {"protected_target_template": "{other}"},
                        {"protected_target_template": "{sender_id!r}"}, {"protected_target_template": "x" * 501},
                        {"protected_target_quote": 1},
                        {"protected_target_policy": "unknown"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_settings({**valid, **changes})
        validate_settings({**DEFAULTS, "protected_target_template": ""})
        validate_settings({**valid, "protected_target_template": "{rule_id} {sender_id} {action}"})
        self.assertEqual(DEFAULTS["protected_target_template"], "有消息触发了规则 {rule_id}, 但目标是管理员/机器人, {action} 无法执行")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root / "state.sqlite", root / "backup")
            config = store.load_or_migrate({})
            for key in ("protected_target_policy", "protected_target_template", "protected_target_quote"):
                config.pop(key)
            with closing(sqlite3.connect(store.path)) as db:
                db.execute("UPDATE dashboard_settings SET payload=? WHERE id=1", (json.dumps(config),))
                db.commit()
            self.assertEqual(store.load_or_migrate({})["protected_target_policy"], "exempt")
            self.assertFalse(store.load_or_migrate({})["protected_target_quote"])
    def test_existing_settings_gain_disabled_execution_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root / "state.sqlite", root / "backup.json")
            original = store.load_or_migrate({})
            original.pop("action_execution_enabled")
            with closing(sqlite3.connect(store.path)) as db:
                db.execute("UPDATE dashboard_settings SET payload=? WHERE id=1", (json.dumps(original),))
                db.commit()
            migrated = store.load_or_migrate({})
            self.assertFalse(migrated["action_execution_enabled"])
            self.assertEqual(store.load_or_migrate({}), migrated)
            with self.assertRaises(ValueError):
                store.save({**migrated, "action_execution_enabled": 1})
    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not installed")
    def test_real_astrbot_empty_schema_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "data" / "config"
            config_dir.mkdir(parents=True)
            config = config_dir / "astrbot_plugin_rule_sentinel_config.json"
            config.write_text(json.dumps({"decision_backend": "jev", "jev_model": "m",
                                          "jev_api_key": "secret"}), encoding="utf-8")
            project = Path(__file__).resolve().parent.parent
            code = """
import importlib, pathlib, sys, types
root = pathlib.Path(sys.argv[1])
config_path = pathlib.Path(sys.argv[2])
package = types.ModuleType('test_plugin')
package.__path__ = [str(root)]
sys.modules['test_plugin'] = package
importlib.import_module('test_plugin.main')
from astrbot.core.config.astrbot_config import AstrBotConfig
AstrBotConfig(config_path=str(config_path), schema={})
from test_plugin.sentinel.settings_store import SettingsStore
data = config_path.parent.parent / 'plugin_data' / 'astrbot_plugin_rule_sentinel'
settings = SettingsStore(data / 'settings.sqlite3', data / 'legacy-settings.json').load_or_migrate({})
assert 'jev_api_key' not in settings
assert settings['decision_backend'] == 'llm'
assert settings['llm_provider_id'] == ''
"""
            env = {**os.environ, "ASTRBOT_ROOT": str(root)}
            result = subprocess.run([sys.executable, "-B", "-c", code, str(project), str(config)],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_capture_migrate_roundtrip_and_reject_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.json"
            old.write_text(json.dumps({"monitoring_enabled": True, "decision_backend": "jev",
                                       "jev_model": "m", "jev_api_key": "secret"}), encoding="utf-8")
            capture_legacy_config(root / "data", old)
            old.write_text("{}", encoding="utf-8")
            store = SettingsStore(root / "db.sqlite3", root / "data" / "legacy-settings.json")
            settings = store.load_or_migrate({})
            self.assertNotIn("jev_api_key", settings)
            self.assertEqual(settings["llm_provider_id"], "")
            self.assertNotIn("monitoring_enabled", settings)
            self.assertFalse(settings["action_execution_enabled"])
            self.assertFalse(store.backup_path.exists())
            settings["model_concurrency"] = 7
            store.save(settings)
            self.assertEqual(store.load_or_migrate(DEFAULTS)["model_concurrency"], 7)
            with self.assertRaises(ValueError):
                store.save({**settings, "model_concurrency": 0})
            self.assertEqual(store.load_or_migrate({})["model_concurrency"], 7)

    def test_bad_backup_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "legacy-settings.json").write_text("{", encoding="utf-8")
            old = root / "old.json"
            old.write_text('{"monitoring_enabled": true}', encoding="utf-8")
            with self.assertRaises(ValueError):
                capture_legacy_config(root, old)
            self.assertIn("true", old.read_text(encoding="utf-8"))
