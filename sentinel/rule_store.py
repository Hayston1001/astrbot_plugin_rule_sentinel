"""Group-isolated versioned rule storage without an external management API.

The caller must authenticate and authorize administrators, present the full
rule for confirmation, and choose a database path in plugin data storage.
This module does not perform any of those operations.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .dry_run_queue import SYSTEM_PLATFORM
from .action_config import CONCRETE_EXECUTION_MODES, migrate_legacy_actions, validate_actions


@dataclass(frozen=True)
class RuleVersion:
    platform_id: str
    group_id: str
    rule_id: str
    version: int
    text: str
    enabled: bool
    allowed_actions: tuple[str, ...]
    duration_mode: str
    severity_seconds: dict[str, int]
    modified_by: str
    created_at: str
    priority: int = 0
    actions: list[dict] = field(default_factory=list)


class VersionConflict(ValueError):
    """The rule changed since the caller displayed it for confirmation."""


class RuleStore:
    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _current_action_mode(db):
        row = db.execute("SELECT payload FROM dashboard_settings WHERE id=1").fetchone()
        config = json.loads(row["payload"]) if row else None
        mode = config.get("default_action_mode") if isinstance(config, dict) else None
        if not isinstance(mode, str) or mode not in CONCRETE_EXECUTION_MODES:
            raise ValueError("global action mode unavailable")
        return mode

    def current_action_mode(self):
        db = self._connect()
        try:
            try:
                return self._current_action_mode(db)
            except sqlite3.Error as error:
                raise ValueError("global action mode unavailable") from error
        finally:
            db.close()

    def initialize(self) -> None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """CREATE TABLE IF NOT EXISTS rule_versions (
                    platform_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    rule_id TEXT NOT NULL, version INTEGER NOT NULL,
                    text TEXT NOT NULL, enabled INTEGER NOT NULL,
                    allowed_actions TEXT NOT NULL, duration_mode TEXT NOT NULL,
                    severity_seconds TEXT NOT NULL, modified_by TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    PRIMARY KEY (platform_id, group_id, rule_id, version)
                )"""
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(rule_versions)")}
            if "priority" not in columns:
                db.execute("ALTER TABLE rule_versions ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            if "actions" not in columns:
                db.execute("ALTER TABLE rule_versions ADD COLUMN actions TEXT")
            rows = db.execute("SELECT rowid, allowed_actions, duration_mode, severity_seconds FROM rule_versions WHERE actions IS NULL").fetchall()
            for row in rows:
                actions = migrate_legacy_actions(json.loads(row["allowed_actions"]), row["duration_mode"],
                                                 json.loads(row["severity_seconds"]))
                db.execute("UPDATE rule_versions SET actions=? WHERE rowid=? AND actions IS NULL",
                           (json.dumps(actions, ensure_ascii=False, sort_keys=True), row["rowid"]))
            db.commit()
        finally:
            db.close()

    def save(
        self, *, platform_id: str, group_id: str, rule_id: str,
        expected_version: int, text: str, enabled: bool,
        modified_by: str, allowed_actions=None, duration_mode=None,
        severity_seconds=None, priority: int = 0, actions=None, expected_action_mode=None,
    ) -> RuleVersion:
        """Append a confirmed version; expected_version=0 creates a rule."""
        self.validate_fields(platform_id=platform_id, group_id=group_id, rule_id=rule_id,
                             expected_version=expected_version, text=text, enabled=enabled,
                             allowed_actions=allowed_actions, duration_mode=duration_mode,
                             severity_seconds=severity_seconds, modified_by=modified_by, priority=priority, actions=actions)
        if actions is None:
            actions = migrate_legacy_actions(allowed_actions, duration_mode, severity_seconds)
        else:
            actions = validate_actions(actions)
            allowed_actions = [action["type"] for action in actions]
            mute = next((action for action in actions if action["type"] == "mute"), None)
            duration_mode = mute["duration_mode"] if mute else "strict"
            severity_seconds = mute["severity_seconds"] if mute else {}

        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            if expected_action_mode is not None and self._current_action_mode(db) != expected_action_mode:
                raise VersionConflict("global action mode changed")
            row = db.execute(
                "SELECT MAX(version) AS version FROM rule_versions WHERE platform_id=? AND group_id=? AND rule_id=?",
                (platform_id, group_id, rule_id),
            ).fetchone()
            current = row["version"] or 0
            if current != expected_version:
                raise VersionConflict("rule version changed")
            db.execute(
                """INSERT INTO rule_versions
                   (platform_id, group_id, rule_id, version, text, enabled, allowed_actions,
                    duration_mode, severity_seconds, modified_by, priority, actions)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (platform_id, group_id, rule_id, current + 1, text, int(enabled),
                 json.dumps(allowed_actions), duration_mode,
                 json.dumps(severity_seconds, sort_keys=True), modified_by, priority,
                 json.dumps(actions, ensure_ascii=False, sort_keys=True)),
            )
            row = db.execute(
                "SELECT * FROM rule_versions WHERE platform_id=? AND group_id=? AND rule_id=? AND version=?",
                (platform_id, group_id, rule_id, current + 1),
            ).fetchone()
            db.commit()
            return self._decode(row)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def validate_fields(*, platform_id: str, group_id: str, rule_id: str,
                        expected_version: int, text: str, enabled: bool,
                        modified_by: str, allowed_actions=None, duration_mode=None,
                        severity_seconds=None, priority: int = 0, actions=None) -> None:
        for value in (platform_id, group_id, rule_id, text, modified_by):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("identity, text and modifier must be nonempty strings")
        if platform_id == SYSTEM_PLATFORM:
            raise ValueError("reserved platform identity")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("invalid expected version")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if type(priority) is not int or not -(2**31) <= priority < 2**31:
            raise ValueError("priority must be a 32-bit integer")
        if actions is None:
            normalized = migrate_legacy_actions(allowed_actions, duration_mode, severity_seconds)
        else:
            if any(value is not None for value in (allowed_actions, duration_mode, severity_seconds)):
                raise ValueError("conflicting action formats")
            normalized = validate_actions(actions)
        if enabled and not normalized:
            raise ValueError("enabled rule requires an action")

    def history(self, platform_id: str, group_id: str, rule_id: str) -> list[RuleVersion]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT * FROM rule_versions WHERE platform_id=? AND group_id=? AND rule_id=? ORDER BY version",
                (platform_id, group_id, rule_id),
            ).fetchall()
            return [self._decode(row) for row in rows]
        finally:
            db.close()

    def current_for_group(self, platform_id: str, group_id: str) -> list[RuleVersion]:
        """Return only the latest version of each rule for this platform and group."""
        db = self._connect()
        try:
            rows = db.execute(
                """SELECT r.* FROM rule_versions r
                   WHERE r.platform_id=? AND r.group_id=? AND r.version=(
                       SELECT MAX(v.version) FROM rule_versions v
                       WHERE v.platform_id=r.platform_id AND v.group_id=r.group_id
                         AND v.rule_id=r.rule_id)
                   ORDER BY r.rule_id""",
                (platform_id, group_id),
            ).fetchall()
            return [self._decode(row) for row in rows]
        finally:
            db.close()

    def enabled_groups(self) -> set[tuple[str, str]]:
        """Identify groups with at least one currently enabled rule."""
        db = self._connect()
        try:
            rows = db.execute(
                """SELECT DISTINCT r.platform_id, r.group_id FROM rule_versions r
                   WHERE r.enabled=1 AND r.version=(
                       SELECT MAX(v.version) FROM rule_versions v
                       WHERE v.platform_id=r.platform_id AND v.group_id=r.group_id
                         AND v.rule_id=r.rule_id)"""
            ).fetchall()
            return {(row["platform_id"], row["group_id"]) for row in rows}
        finally:
            db.close()

    @staticmethod
    def _decode(row: sqlite3.Row) -> RuleVersion:
        if type(row["enabled"]) is not int or row["enabled"] not in (0, 1):
            raise ValueError("corrupt rule enabled state")
        legacy_actions = json.loads(row["allowed_actions"])
        severity = json.loads(row["severity_seconds"])
        actions = (validate_actions(json.loads(row["actions"])) if "actions" in row.keys() and row["actions"] is not None
                   else migrate_legacy_actions(legacy_actions, row["duration_mode"], severity))
        RuleStore.validate_fields(
            platform_id=row["platform_id"], group_id=row["group_id"], rule_id=row["rule_id"],
            expected_version=row["version"], text=row["text"], enabled=bool(row["enabled"]),
            actions=actions,
            modified_by=row["modified_by"], priority=row["priority"],
        )
        if row["version"] <= 0:
            raise ValueError("corrupt rule version")
        return RuleVersion(
            row["platform_id"], row["group_id"], row["rule_id"], row["version"],
            row["text"], bool(row["enabled"]), tuple(action["type"] for action in actions),
            row["duration_mode"], severity,
            row["modified_by"], row["created_at"], row["priority"], actions,
        )
