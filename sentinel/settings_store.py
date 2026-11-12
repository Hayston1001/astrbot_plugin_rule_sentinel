"""Persistent dashboard settings and one-time legacy configuration capture."""

import copy
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from string import Formatter

from .plugin_settings import read_settings


DEFAULTS = {
    "queue_capacity_per_group": 100, "ingress_capacity": 256,
    "action_execution_enabled": True, "monitoring_mode": "allowlist", "monitoring_groups": [],
    "max_duration_seconds": 2592000, "model_concurrency": 20, "model_timeout_seconds": 35,
    "global_rate_per_minute": 100, "group_rate_per_minute": 60, "model_failure_threshold": 5,
    "model_cooldown_seconds": 120, "notification_target": "", "notification_interval_seconds": 0,
    "decision_backend": "llm", "rule_selection_strategy": "model", "llm_provider_id": "",
    "protected_target_policy": "exempt", "protected_target_template": "有消息触发了规则 {rule_id}, 但目标是管理员/机器人, {action} 无法执行",
    "protected_target_quote": False,
    "default_execution_mode": "execute", "default_action_mode": "auto", "group_overrides": {},
    "message_retention_days": 30, "llm_reason_language": "", "llm_no_action_reason_enabled": True,
}

MIGRATED_DEFAULTS = {key: DEFAULTS[key] for key in ("action_execution_enabled", "protected_target_policy",
    "protected_target_template", "protected_target_quote", "default_execution_mode", "default_action_mode", "group_overrides",
    "message_retention_days", "llm_reason_language", "llm_no_action_reason_enabled")}
MIGRATED_DEFAULTS["action_execution_enabled"] = False

LEGACY_DEFAULTS = {
    "action_execution_enabled": False, "decision_backend": "",
    "model_concurrency": 2, "model_timeout_seconds": 30,
    "global_rate_per_minute": 60, "group_rate_per_minute": 30, "model_cooldown_seconds": 60,
}


def capture_legacy_config(data_dir: Path, config_path: Path) -> None:
    """Preserve old settings before AstrBot applies an empty config schema.

    AstrBot imports the plugin before it normalizes and rewrites its config.
    The exclusive backup is retained until the database migration succeeds.
    """
    backup = data_dir / "legacy-settings.json"
    if backup.exists():
        with backup.open(encoding="utf-8") as source:
            if not isinstance(json.load(source), dict):
                raise ValueError("invalid legacy backup")
        return
    if not config_path.is_file():
        return
    with config_path.open(encoding="utf-8-sig") as source:
        config = json.load(source)
    if not isinstance(config, dict):
        raise ValueError("invalid legacy settings")
    legacy = {key: config[key] for key in (*DEFAULTS, "monitoring_enabled") if key in config}
    if not legacy:
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="legacy-settings-", suffix=".tmp", dir=data_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            json.dump(legacy, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        if not backup.exists():
            os.replace(temporary, backup)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_settings(config):
    if not isinstance(config, dict) or set(config) != set(DEFAULTS):
        raise ValueError("invalid settings fields")
    # A model may be selected later; runtime callers still require one.
    read_settings(config, require_model=False)
    from .message_retention import validate_retention_days
    validate_retention_days(config["message_retention_days"])
    from .group_settings import validate_reason_language
    validate_reason_language(config["llm_reason_language"])
    if type(config["action_execution_enabled"]) is not bool:
        raise ValueError("invalid action execution setting")
    validate_protected_target_settings(config)
    for key in ("llm_provider_id", "notification_target"):
        if not isinstance(config[key], str):
            raise ValueError("invalid settings text")
    interval = config["notification_interval_seconds"]
    if type(interval) is not int or interval < 0 or bool(config["notification_target"].strip()) != (interval > 0):
        raise ValueError("invalid notification settings")
    from .group_settings import resolve_group_settings, validate_group_key
    overrides = config["group_overrides"]
    if (config["default_execution_mode"] not in ("record", "execute")
            or config["default_action_mode"] not in ("auto", "confirm") or not isinstance(overrides, dict)):
        raise ValueError("invalid group defaults")
    for key in overrides:
        validate_group_key(key)
        resolve_group_settings(config, *key.split("/"))


def validate_protected_target_settings(config):
    policy, template = config["protected_target_policy"], config["protected_target_template"]
    if policy not in ("exempt", "group_message") or not isinstance(template, str) or len(template) > 500:
        raise ValueError("invalid protected target settings")
    if policy == "group_message" and not template.strip():
        raise ValueError("protected target template required")
    if type(config["protected_target_quote"]) is not bool:
        raise ValueError("invalid protected target flags")
    try:
        for _, field, spec, conversion in Formatter().parse(template):
            if field is not None and (field not in ("sender_id", "rule_id", "action") or spec or conversion):
                raise ValueError("invalid placeholder")
    except (TypeError, ValueError) as error:
        raise ValueError("invalid protected target template") from error


class SettingsStore:
    def __init__(self, path, backup_path):
        self.path = str(path)
        self.backup_path = Path(backup_path)

    @staticmethod
    def _retire_disabled_work(db):
        # Do not revive old actions or notices when a disabled legacy setting is removed.
        for table in ("execution_intents", "protected_target_notices"):
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                continue
            states = ("pending_auto", "pending_confirm", "pending_retry") if table == "execution_intents" else ("pending", "pending_retry")
            placeholders = ",".join("?" for _ in states)
            db.execute(f"UPDATE {table} SET status='inactive', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                       f"WHERE status IN ({placeholders})", states)

    @staticmethod
    def _remove_retired_backend(db, config):
        """Remove old model settings without silently switching monitored groups."""
        changed = False
        for key in ("jev_model", "jev_api_key", "jev_provider"):
            if key in config:
                del config[key]
                changed = True
        global_retired = config.get("decision_backend") == "jev"
        if global_retired:
            config["decision_backend"] = "llm"
            config["llm_provider_id"] = ""
            changed = True
        overrides = config.get("group_overrides", {})
        retired_groups = []
        if isinstance(overrides, dict):
            from .group_settings import validate_group_key
            for key, group in list(overrides.items()):
                selected = group.get("model") if isinstance(group, dict) else None
                if isinstance(selected, dict) and selected.get("backend") == "jev":
                    validate_group_key(key)
                    if (set(selected) != {"backend", "model"} or not isinstance(selected["model"], str)
                            or not selected["model"].strip() or len(selected["model"]) > 128):
                        raise ValueError("invalid group model")
                    retired_groups.append(key)
                    del group["model"]
                    changed = True
        if retired_groups:
            groups = config.get("monitoring_groups")
            mode = config.get("monitoring_mode")
            if not isinstance(groups, list) or mode not in ("allowlist", "blocklist"):
                raise ValueError("invalid monitoring settings")
            for key in retired_groups:
                if mode == "allowlist":
                    groups[:] = [group for group in groups if group != key]
                elif key not in groups:
                    groups.append(key)
        if not global_retired and not retired_groups:
            return changed
        if global_retired:
            SettingsStore._retire_disabled_work(db)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        scopes = [None] if global_retired else [tuple(key.split("/")) for key in retired_groups]
        for scope in scopes:
            where = "" if scope is None else " AND platform_id=? AND group_id=?"
            args = () if scope is None else scope
            if scope is not None and "execution_intents" in tables:
                if "protected_target_notices" in tables:
                    db.execute("UPDATE protected_target_notices SET status='inactive', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                               "WHERE status IN ('pending', 'pending_retry') AND intent_id IN "
                               "(SELECT id FROM execution_intents WHERE platform_id=? AND group_id=?)", args)
                db.execute("UPDATE execution_intents SET status='inactive', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                           "WHERE status IN ('pending_auto', 'pending_confirm', 'pending_retry')" + where, args)
            if {"dry_run_queue", "dry_run_events"} <= tables:
                db.execute("INSERT INTO dry_run_events (platform_id, group_id, message_id, status, detail) "
                           "SELECT platform_id, group_id, message_id, 'review', 'decision_model_removed' "
                           "FROM dry_run_queue WHERE state IN ('pending', 'processing')" + where, args)
                db.execute("UPDATE dry_run_queue SET state='done', payload=NULL, lease=NULL "
                           "WHERE state IN ('pending', 'processing')" + where, args)
        return changed

    def load_or_migrate(self, legacy):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS dashboard_settings (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
            row = db.execute("SELECT payload FROM dashboard_settings WHERE id=1").fetchone()
            if row is None:
                has_backup = self.backup_path.is_file()
                if has_backup:
                    with self.backup_path.open(encoding="utf-8") as source:
                        legacy = json.load(source)
                if not isinstance(legacy, dict):
                    raise ValueError("invalid legacy settings")
                if "monitoring_enabled" in legacy and type(legacy["monitoring_enabled"]) is not bool:
                    raise ValueError("invalid legacy monitoring setting")
                captured = {key: legacy[key] for key in DEFAULTS if key in legacy}
                config = copy.deepcopy(DEFAULTS)
                if has_backup or captured or "monitoring_enabled" in legacy:
                    # Preserve the old defaults and the legacy execution safety boundary.
                    config.update(LEGACY_DEFAULTS)
                    config.update(captured)
                    config["action_execution_enabled"] = False
                if legacy.get("monitoring_enabled") is False:
                    self._retire_disabled_work(db)
                self._remove_retired_backend(db, config)
                payload = json.dumps(config, ensure_ascii=False, allow_nan=False)
                db.execute("INSERT INTO dashboard_settings(id, payload) VALUES (1, ?)", (payload,))
            else:
                config = json.loads(row[0])
                if not isinstance(config, dict):
                    raise ValueError("invalid stored settings")
                removed = "monitoring_enabled" in config
                legacy_enabled = config.pop("monitoring_enabled") if removed else None
                if removed and type(legacy_enabled) is not bool:
                    raise ValueError("invalid legacy monitoring setting")
                retired = self._remove_retired_backend(db, config)
                missing = set(DEFAULTS) - set(config)
                if set(config) - set(DEFAULTS) or missing - set(MIGRATED_DEFAULTS):
                    raise ValueError("invalid stored settings")
                config.update({key: copy.deepcopy(MIGRATED_DEFAULTS[key]) for key in missing})
                if legacy_enabled is False:
                    self._retire_disabled_work(db)
                if removed or missing or retired:
                    db.execute("UPDATE dashboard_settings SET payload=? WHERE id=1",
                               (json.dumps(config, ensure_ascii=False, allow_nan=False),))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
        # This file was created by the migration capture and is no longer needed.
        self.backup_path.unlink(missing_ok=True)
        return config

    def save(self, config, *, remember_group=None):
        validate_settings(config)
        if remember_group is not None:
            from .group_settings import validate_group_key
            validate_group_key(remember_group)
        payload = json.dumps(config, ensure_ascii=False, allow_nan=False)
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("BEGIN IMMEDIATE")
            previous_row = db.execute("SELECT payload FROM dashboard_settings WHERE id=1").fetchone()
            previous = json.loads(previous_row[0]) if previous_row else None
            reason_key = "llm_no_action_reason_enabled"
            reason_only = (remember_group is None and isinstance(previous, dict)
                           and type(previous.get(reason_key)) is bool and previous[reason_key] is not config[reason_key]
                           and {**previous, reason_key: config[reason_key]} == config)
            if db.execute("UPDATE dashboard_settings SET payload=? WHERE id=1", (payload,)).rowcount != 1:
                raise RuntimeError("settings row is missing")
            if not reason_only:
                from .message_retention import purge_expired_contents
                purge_expired_contents(db, config["message_retention_days"])
                if remember_group is not None:
                    db.execute("INSERT OR IGNORE INTO dashboard_group_names (platform_id, group_id) VALUES (?, ?)",
                               tuple(remember_group.split("/")))
                if config["action_execution_enabled"] is False and db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_intents'").fetchone():
                    db.execute("""UPDATE execution_intents SET status='inactive', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                        WHERE status IN ('pending_auto', 'pending_confirm', 'pending_retry')""")
                elif db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_intents'").fetchone():
                    from .group_settings import decision_fingerprint, execution_allowed
                    groups = db.execute("SELECT DISTINCT platform_id, group_id FROM execution_intents "
                                        "WHERE status IN ('pending_auto', 'pending_confirm', 'pending_retry')").fetchall()
                    for platform_id, group_id in groups:
                        try:
                            unchanged = (decision_fingerprint(previous, platform_id, group_id)
                                         == decision_fingerprint(config, platform_id, group_id))
                        except (TypeError, ValueError, KeyError):
                            unchanged = False
                        if not unchanged or not execution_allowed(config, platform_id, group_id):
                            db.execute("""UPDATE execution_intents SET status='inactive', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                                WHERE platform_id=? AND group_id=? AND status IN ('pending_auto', 'pending_confirm', 'pending_retry')""",
                                       (platform_id, group_id))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
