"""Persistent dry-run queue; never executes platform actions.

Pending payloads contain sensitive message text in SQLite. The application must
obtain an explicit retention decision before enabling this component and place
the database in the AstrBot plugin data directory. SQLite may retain deleted
bytes in its database or journal; clearing a payload is not secure erasure.
Recovery requires exclusive ownership of the queue and stopped old workers.
"""

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .prompts import PROMPTS

SYSTEM_PLATFORM = "__rule_sentinel_system__"
OVERFLOW_GROUP = "__ingress_overflow__"
MAX_AUDIT_DETAIL_CHARS = 100000
SUMMARY_LIMIT = 5000


@dataclass(frozen=True)
class WorkItem:
    platform_id: str
    group_id: str
    message_id: str
    payload: dict
    lease: str


class DryRunQueue:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.event_sink = None

    def _publish(self, key, event_id):
        if self.event_sink is not None:
            self.event_sink(*key, event_id)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def initialize(self):
        db = self._connect()
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS dry_run_queue (
                    id INTEGER PRIMARY KEY,
                    platform_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, payload TEXT,
                    state TEXT NOT NULL, lease TEXT,
                    UNIQUE(platform_id, group_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS dry_run_queue_group_state
                    ON dry_run_queue(platform_id, group_id, state, id);
                CREATE TABLE IF NOT EXISTS dry_run_events (
                    id INTEGER PRIMARY KEY,
                    platform_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, status TEXT NOT NULL,
                    detail TEXT NOT NULL, attempt_id TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                CREATE INDEX IF NOT EXISTS dry_run_events_group_message_id
                    ON dry_run_events(platform_id, group_id, message_id, id);
                CREATE TABLE IF NOT EXISTS execution_intents (
                    id INTEGER PRIMARY KEY, source_event_id INTEGER NOT NULL UNIQUE,
                    source_attempt_id TEXT NOT NULL, platform_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, rule_id TEXT NOT NULL, rule_version INTEGER NOT NULL,
                    action TEXT NOT NULL, target_sender_id TEXT NOT NULL, mode TEXT NOT NULL,
                    duration_seconds INTEGER, expires_at TEXT, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, platform_code TEXT, trace_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_attempts (
                    id INTEGER PRIMARY KEY, intent_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
                    status TEXT NOT NULL, expires_at TEXT NOT NULL, platform_code TEXT, trace_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(intent_id, attempt)
                );
                CREATE TABLE IF NOT EXISTS protected_target_notices (
                    intent_id INTEGER PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    template TEXT NOT NULL, quote INTEGER NOT NULL,
                    sent_quoted INTEGER, sent_passive INTEGER, send_method TEXT, downgraded INTEGER NOT NULL DEFAULT 0, downgrade_reason TEXT,
                    platform_code TEXT, reason_code TEXT, trace_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS protected_target_notice_attempts (
                    intent_id INTEGER NOT NULL, attempt INTEGER NOT NULL, status TEXT NOT NULL,
                    sent_quoted INTEGER NOT NULL, sent_passive INTEGER NOT NULL, send_method TEXT NOT NULL, downgraded INTEGER NOT NULL DEFAULT 0, downgrade_reason TEXT,
                    platform_code TEXT, reason_code TEXT, trace_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(intent_id, attempt)
                );
                CREATE TABLE IF NOT EXISTS dashboard_ui_state (
                    username TEXT PRIMARY KEY, platform_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    active_tab TEXT NOT NULL DEFAULT 'status'
                );
                CREATE TABLE IF NOT EXISTS dashboard_overview_layout (
                    username TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dashboard_quick_start (
                    id INTEGER PRIMARY KEY CHECK(id = 1)
                );
                CREATE TABLE IF NOT EXISTS dashboard_group_names (
                    platform_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    alias TEXT NOT NULL DEFAULT '', platform_name TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(platform_id, group_id)
                );
                CREATE TABLE IF NOT EXISTS message_contents (
                    platform_id TEXT NOT NULL, group_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    stored_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    PRIMARY KEY(platform_id, group_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS message_contents_stored_at ON message_contents(stored_at);
                CREATE TABLE IF NOT EXISTS notification_events (
                    id INTEGER PRIMARY KEY, code TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
            """)
            # SQLite databases created by early development versions lack the
            # attempt column. Serialize the migration across processes.
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(dashboard_ui_state)")}
            if "active_tab" not in columns:
                db.execute("ALTER TABLE dashboard_ui_state ADD COLUMN active_tab TEXT NOT NULL DEFAULT 'status'")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(dry_run_events)")}
            if "attempt_id" not in columns:
                db.execute("ALTER TABLE dry_run_events ADD COLUMN attempt_id TEXT")
            for table in ("execution_intents", "execution_attempts"):
                columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                if "reason_code" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN reason_code TEXT")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(execution_intents)")}
            if "message_time" not in columns:
                db.execute("ALTER TABLE execution_intents ADD COLUMN message_time TEXT")
            if "action_index" not in columns:
                db.execute("""CREATE TABLE execution_intents_plan (
                    id INTEGER PRIMARY KEY, source_event_id INTEGER NOT NULL,
                    source_attempt_id TEXT NOT NULL, platform_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, rule_id TEXT NOT NULL, rule_version INTEGER NOT NULL,
                    action TEXT NOT NULL, target_sender_id TEXT NOT NULL, mode TEXT NOT NULL,
                    duration_seconds INTEGER, expires_at TEXT, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, platform_code TEXT, trace_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, reason_code TEXT, message_time TEXT,
                    plan_id TEXT NOT NULL, action_index INTEGER NOT NULL CHECK(action_index >= 0),
                    UNIQUE(source_event_id, action_index), UNIQUE(plan_id, action_index)
                )""")
                db.execute("""INSERT INTO execution_intents_plan
                    SELECT id, source_event_id, source_attempt_id, platform_id, group_id,
                    message_id, rule_id, rule_version, action, target_sender_id, mode,
                    duration_seconds, expires_at, status, attempts, platform_code, trace_id,
                    created_at, updated_at, reason_code, message_time,
                    'event:' || source_event_id, 0 FROM execution_intents""")
                db.execute("DROP TABLE execution_intents")
                db.execute("ALTER TABLE execution_intents_plan RENAME TO execution_intents")
            db.execute("CREATE INDEX IF NOT EXISTS execution_intents_pending_group "
                       "ON execution_intents(status, action, platform_id, group_id, id)")
            db.commit()
        finally:
            db.close()

    def enqueue(self, *, platform_id: str, group_id: str, message_id: str,
                payload: dict, capacity: int) -> str:
        if any(not isinstance(x, str) or not x.strip() for x in (platform_id, group_id, message_id)):
            raise ValueError("missing message identity")
        if type(capacity) is not int or capacity <= 0 or not isinstance(payload, dict):
            raise ValueError("invalid capacity or payload")
        queued_payload = {key: value for key, value in payload.items() if key != "original_text"}
        encoded = json.dumps(queued_payload, ensure_ascii=False, allow_nan=False)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            key = (platform_id, group_id, message_id)
            if db.execute("SELECT 1 FROM dry_run_queue WHERE platform_id=? AND group_id=? AND message_id=?", key).fetchone():
                db.commit()
                return "duplicate"
            count = db.execute("SELECT COUNT(*) FROM dry_run_queue WHERE platform_id=? AND group_id=? AND state IN ('pending', 'processing')", key[:2]).fetchone()[0]
            if count >= capacity:
                event = db.execute("INSERT INTO dry_run_events (platform_id, group_id, message_id, status, detail) VALUES (?, ?, ?, 'skipped', 'queue_full')", key)
                db.commit()
                self._publish(key, event.lastrowid)
                return "queue_full"
            db.execute("INSERT INTO dry_run_queue (platform_id, group_id, message_id, payload, state) VALUES (?, ?, ?, ?, 'pending')", (*key, encoded))
            from .message_retention import stored_retention_days
            days = stored_retention_days(db)
            text = payload.get("original_text", payload.get("text"))
            if days and isinstance(text, str) and len(text) <= 65536:
                db.execute("INSERT INTO message_contents (platform_id, group_id, message_id, content) VALUES (?, ?, ?, ?)", (*key, text))
            db.commit()
            return "queued"
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def claim(self, platform_id: str, group_id: str) -> WorkItem | None:
        db = self._connect()
        events = []
        try:
            db.execute("BEGIN IMMEDIATE")
            key = (platform_id, group_id)
            if db.execute("SELECT 1 FROM dry_run_queue WHERE platform_id=? AND group_id=? AND state='processing'", key).fetchone():
                db.commit()
                return None
            while True:
                row = db.execute("SELECT * FROM dry_run_queue WHERE platform_id=? AND group_id=? AND state='pending' ORDER BY id LIMIT 1", key).fetchone()
                if row is None:
                    db.commit()
                    for event_key, event_id in events:
                        self._publish(event_key, event_id)
                    return None
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, ValueError, UnicodeDecodeError, RecursionError):
                    payload = None
                if not isinstance(payload, dict):
                    # Quarantine malformed data atomically with its audit entry.
                    # Never copy the invalid payload or decoder error to audit.
                    db.execute("UPDATE dry_run_queue SET state='done', payload=NULL, lease=NULL WHERE id=?", (row["id"],))
                    event = db.execute("INSERT INTO dry_run_events (platform_id, group_id, message_id, status, detail) VALUES (?, ?, ?, 'failed', 'invalid_queue_payload')",
                               (row["platform_id"], row["group_id"], row["message_id"]))
                    events.append(((row["platform_id"], row["group_id"], row["message_id"]), event.lastrowid))
                    continue
                lease = uuid.uuid4().hex
                db.execute("UPDATE dry_run_queue SET state='processing', lease=? WHERE id=?", (lease, row["id"]))
                db.commit()
                for event_key, event_id in events:
                    self._publish(event_key, event_id)
                return WorkItem(row["platform_id"], row["group_id"], row["message_id"], payload, lease)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def record_evaluation_start(self, item: WorkItem, detail: str) -> None:
        """Persist the exact rule snapshot before calling a decision provider."""
        if not isinstance(detail, str) or not detail.strip() or len(detail) > MAX_AUDIT_DETAIL_CHARS:
            raise ValueError("invalid evaluation snapshot")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            key = (item.platform_id, item.group_id, item.message_id)
            row = db.execute(
                "SELECT 1 FROM dry_run_queue WHERE platform_id=? AND group_id=? AND message_id=? AND state='processing' AND lease=?",
                (*key, item.lease),
            ).fetchone()
            if row is None:
                raise RuntimeError("queue lease lost before audit")
            event = db.execute(
                "INSERT INTO dry_run_events (platform_id, group_id, message_id, status, detail, attempt_id) VALUES (?, ?, ?, 'started', ?, ?)",
                (*key, detail, item.lease),
            )
            db.commit()
            self._publish(key, event.lastrowid)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def return_unstarted(self, item: WorkItem) -> bool:
        """Release a claimed item before model work begins, preserving its order."""
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                """UPDATE dry_run_queue SET state='pending', lease=NULL
                   WHERE platform_id=? AND group_id=? AND message_id=?
                     AND state='processing' AND lease=?""",
                (item.platform_id, item.group_id, item.message_id, item.lease),
            )
            db.commit()
            return result.rowcount == 1
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def finish(self, item: WorkItem, *, status: str, detail: str,
               rule_versions: tuple[tuple[str, int], ...] | None = None,
               settings_fingerprint: str | None = None) -> bool:
        """Append an audit event and clear the payload in one transaction."""
        if status not in {"proposed", "no_action", "rejected", "failed", "timed_out", "skipped", "review"}:
            raise ValueError("invalid dry-run status")
        if not isinstance(detail, str) or not detail.strip() or len(detail) > MAX_AUDIT_DETAIL_CHARS:
            raise ValueError("invalid audit detail")
        if settings_fingerprint is not None and (not isinstance(settings_fingerprint, str)
                                                 or len(settings_fingerprint) != 64):
            raise ValueError("invalid settings fingerprint")
        if rule_versions is not None:
            if (status not in ("proposed", "no_action") or not isinstance(rule_versions, tuple)
                    or not rule_versions or any(not isinstance(entry, tuple) or len(entry) != 2
                        or not isinstance(entry[0], str) or not entry[0].strip()
                        or type(entry[1]) is not int or entry[1] <= 0 for entry in rule_versions)
                    or len({entry[0] for entry in rule_versions}) != len(rule_versions)):
                raise ValueError("invalid rule version snapshot")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            key = (item.platform_id, item.group_id, item.message_id)
            if status in ("failed", "timed_out"):
                started = db.execute("""SELECT detail FROM dry_run_events
                    WHERE platform_id=? AND group_id=? AND message_id=? AND attempt_id=? AND status='started'
                    ORDER BY id DESC LIMIT 1""", (*key, item.lease)).fetchone()
                if started:
                    try:
                        start_detail = json.loads(started["detail"])
                    except (ValueError, TypeError, RecursionError):
                        start_detail = None
                    if isinstance(start_detail, dict) and start_detail.get("prompt_language") in PROMPTS:
                        try:
                            terminal = json.loads(detail)
                        except (ValueError, TypeError, RecursionError):
                            terminal = None
                        if not isinstance(terminal, dict):
                            terminal = {"reason": detail}
                        terminal["prompt_language"] = start_detail["prompt_language"]
                        try:
                            updated = json.dumps(terminal, ensure_ascii=False, allow_nan=False)
                        except (TypeError, ValueError, RecursionError):
                            updated = ""
                        if updated and len(updated) <= MAX_AUDIT_DETAIL_CHARS:
                            detail = updated
            if rule_versions is not None:
                rows = db.execute("""SELECT r.rule_id, r.version FROM rule_versions r
                    WHERE r.platform_id=? AND r.group_id=? AND r.enabled=1 AND r.version=(
                        SELECT MAX(v.version) FROM rule_versions v
                        WHERE v.platform_id=r.platform_id AND v.group_id=r.group_id AND v.rule_id=r.rule_id)""",
                    key[:2]).fetchall()
                current = {row["rule_id"]: row["version"] for row in rows}
                if current != dict(rule_versions):
                    try:
                        previous = json.loads(detail)
                    except (ValueError, TypeError, RecursionError):
                        previous = None
                    review = {"review_reason": "rule_snapshot_changed_at_commit", "original_status": status}
                    if isinstance(previous, dict):
                        review["decision"] = previous
                    try:
                        detail = json.dumps(review, ensure_ascii=False, allow_nan=False)
                    except (ValueError, TypeError, RecursionError):
                        detail = json.dumps({"review_reason": "rule_snapshot_changed_at_commit",
                                             "original_status": status})
                    if len(detail) > MAX_AUDIT_DETAIL_CHARS:
                        detail = json.dumps({"review_reason": "rule_snapshot_changed_at_commit",
                                             "original_status": status})
                    status = "review"
            if settings_fingerprint is not None and status in ("proposed", "no_action"):
                from .group_settings import decision_fingerprint
                try:
                    row = db.execute("SELECT payload FROM dashboard_settings WHERE id=1").fetchone()
                    current_settings = json.loads(row["payload"]) if row else None
                    current_fingerprint = decision_fingerprint(current_settings, *key[:2])
                except (ValueError, TypeError, KeyError, sqlite3.Error):
                    current_fingerprint = None
                if current_fingerprint != settings_fingerprint:
                    try:
                        previous = json.loads(detail)
                    except (ValueError, TypeError, RecursionError):
                        previous = None
                    review = {"review_reason": "group_settings_changed_at_commit", "original_status": status}
                    if isinstance(previous, dict):
                        review["decision"] = previous
                    detail = json.dumps(review, ensure_ascii=False, allow_nan=False)
                    if len(detail) > MAX_AUDIT_DETAIL_CHARS:
                        detail = json.dumps({"review_reason": "group_settings_changed_at_commit", "original_status": status})
                    status = "review"
            source = db.execute("SELECT payload FROM dry_run_queue WHERE platform_id=? AND group_id=? AND message_id=? AND state='processing' AND lease=?", (*key, item.lease)).fetchone()
            result = db.execute("UPDATE dry_run_queue SET state='done', payload=NULL, lease=NULL WHERE platform_id=? AND group_id=? AND message_id=? AND state='processing' AND lease=?", (*key, item.lease))
            if result.rowcount != 1:
                db.rollback()
                return False
            event = db.execute("INSERT INTO dry_run_events (platform_id, group_id, message_id, status, detail, attempt_id) VALUES (?, ?, ?, ?, ?, ?)", (*key, status, detail, item.lease))
            if status == "proposed" and rule_versions is not None:
                self._record_execution_intent(db, item, source["payload"], event.lastrowid, detail)
            db.commit()
            self._publish(key, event.lastrowid)
            return True
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _record_execution_intent(db, item, payload, event_id, detail, action_index=0):
        """Create an intent atomically with its validated audit event.

        The executor rechecks durable settings, rule, permissions and intent
        state before acting.
        """
        from .rule_store import RuleStore
        from .mute_validation import revalidate_mute
        from .recall_validation import revalidate_recall

        try:
            snapshot = json.loads(payload)
            proposal = json.loads(detail)["proposal"]
            sender = snapshot["sender_id"]
            if not isinstance(sender, str) or not sender.strip() or proposal["target_sender_id"] != sender:
                raise ValueError("sender mismatch")
            rule_id, version = proposal["rule_id"], proposal["rule_version"]
            if not isinstance(rule_id, str) or not rule_id.strip() or type(version) is not int or version <= 0:
                raise ValueError("invalid rule identity")
            row = db.execute("SELECT * FROM rule_versions WHERE platform_id=? AND group_id=? AND rule_id=? ORDER BY version DESC LIMIT 1",
                             (item.platform_id, item.group_id, rule_id)).fetchone()
            rule = RuleStore._decode(row) if row is not None else None
            if "action" not in proposal:
                if rule is None or rule.version != version or not rule.enabled:
                    raise ValueError("invalid action rule")
                for index, entry in enumerate(rule.actions):
                    action_proposal = {**proposal, "action": entry["type"], "mode": entry["mode"]}
                    if entry["type"] != "mute":
                        action_proposal["duration_seconds"] = None
                    DryRunQueue._record_execution_intent(db, item, payload, event_id,
                        json.dumps({"proposal": action_proposal}, ensure_ascii=False), action_index=index)
                return
            action = proposal["action"]
            if action not in ("mute", "reply", "recall") or rule is None or rule.version != version or not rule.enabled:
                raise ValueError("invalid action rule")
            selected = next((entry for entry in rule.actions if entry["type"] == action), None)
            if selected is None or proposal.get("mode") != selected["mode"]:
                raise ValueError("action not allowed")
            has_settings = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dashboard_settings'").fetchone()
            stored_settings = db.execute("SELECT payload FROM dashboard_settings WHERE id=1").fetchone() if has_settings else None
            settings = json.loads(stored_settings["payload"]) if stored_settings else {}
            from .group_settings import execution_allowed
            from .action_config import resolve_action_mode
            execution_enabled = execution_allowed(settings, item.platform_id, item.group_id)
            default_action_mode = settings.get("default_action_mode")
            actual_mode = resolve_action_mode(selected["mode"], default_action_mode)
            limit = settings.get("max_duration_seconds", 2592000)
            now = datetime.now(timezone.utc)
            duration = proposal.get("duration_seconds")
            expires = None
            message_time = None
            missing_time = False
            try:
                parsed_time = datetime.fromisoformat(snapshot["message_time"])
                if parsed_time.tzinfo is None or parsed_time > now:
                    raise ValueError("invalid message timestamp")
                message_time = parsed_time.astimezone(timezone.utc).isoformat(timespec="milliseconds")
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
            intent = {"platform_id": item.platform_id, "group_id": item.group_id, "message_id": item.message_id, "rule_id": rule_id,
                      "rule_version": version, "action": action, "target_sender_id": sender,
                      "duration_seconds": duration, "mode": actual_mode}
            if action == "mute":
                if not revalidate_mute(intent, rule=rule, max_duration_seconds=limit, default_action_mode=default_action_mode).allowed:
                    raise ValueError("invalid mute")
                expires = (now + timedelta(seconds=duration)).isoformat(timespec="seconds")
            elif action == "recall":
                if not revalidate_recall(intent, rule=rule, default_action_mode=default_action_mode).allowed:
                    raise ValueError("invalid recall")
                duration = None
                try:
                    if message_time is None:
                        raise ValueError("invalid message timestamp")
                    expires = (datetime.fromisoformat(message_time) + timedelta(seconds=110)).isoformat(timespec="milliseconds")
                except (TypeError, ValueError, OverflowError):
                    missing_time = True
                    message_time = None
            else:
                duration = None
            timestamp = now.isoformat(timespec="milliseconds")
            db.execute("""INSERT INTO execution_intents
                (source_event_id, source_attempt_id, platform_id, group_id, message_id, rule_id, rule_version,
                 action, target_sender_id, mode, duration_seconds, expires_at, status, created_at, updated_at, message_time, reason_code, plan_id, action_index)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, item.lease, item.platform_id, item.group_id, item.message_id, rule_id, version,
                 action, sender, actual_mode, duration, expires,
                 "review" if action == "recall" and execution_enabled and missing_time else
                 ("pending_auto" if actual_mode == "auto" else "pending_confirm") if execution_enabled
                 else "inactive", timestamp, timestamp, message_time,
                 "invalid_message_time" if action == "recall" and execution_enabled and missing_time else None,
                 f"event:{event_id}", action_index))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("invalid execution intent") from error

    def recover_after_exclusive_start(self) -> int:
        """Reset dry-run work after all previous workers have stopped."""
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute("UPDATE dry_run_queue SET state='pending', lease=NULL WHERE state='processing'")
            db.commit()
            return result.rowcount
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def record_skip(self, *, platform_id: str, group_id: str, message_id: str, reason: str) -> None:
        """Record a skip without storing message contents."""
        if any(not isinstance(x, str) or not x.strip() for x in (platform_id, group_id, message_id, reason)):
            raise ValueError("invalid skip metadata")
        if len(reason) > 200:
            raise ValueError("invalid skip reason")
        db = self._connect()
        try:
            event = db.execute("INSERT INTO dry_run_events (platform_id, group_id, message_id, status, detail) VALUES (?, ?, ?, 'skipped', ?)",
                       (platform_id, group_id, message_id, reason))
            db.commit()
            self._publish((platform_id, group_id, message_id), event.lastrowid)
        finally:
            db.close()

    def pending_auto_intent(self, item):
        db = self._connect()
        try:
            row = db.execute("""SELECT id FROM execution_intents WHERE platform_id=? AND group_id=?
                AND message_id=? AND source_attempt_id=? AND status='pending_auto' ORDER BY action_index LIMIT 1""",
                (item.platform_id, item.group_id, item.message_id, item.lease)).fetchone()
            return row["id"] if row else None
        finally:
            db.close()

    def publish_intent(self, intent_id):
        db = self._connect()
        try:
            row = db.execute("SELECT platform_id, group_id, message_id, source_event_id FROM execution_intents WHERE id=?", (intent_id,)).fetchone()
            if row:
                self._publish((row["platform_id"], row["group_id"], row["message_id"]), row["source_event_id"])
        finally:
            db.close()

    @staticmethod
    def _execution_details(db, row):
        result = dict(row)
        result["history"] = [dict(attempt) for attempt in db.execute("""SELECT attempt, status, expires_at,
            platform_code, reason_code, trace_id, created_at, updated_at FROM execution_attempts WHERE intent_id=? ORDER BY attempt""",
            (row["id"],)).fetchall()]
        notice = db.execute("""SELECT status, attempts, sent_quoted, sent_passive, send_method, downgraded, downgrade_reason, platform_code, reason_code, trace_id, updated_at
            FROM protected_target_notices WHERE intent_id=?""", (row["id"],)).fetchone()
        result["notice"] = dict(notice) if notice else None
        if notice:
            result["notice"]["history"] = [dict(attempt) for attempt in db.execute("""SELECT attempt, status, sent_quoted, sent_passive, send_method, downgraded, downgrade_reason,
                platform_code, reason_code, trace_id, created_at, updated_at FROM protected_target_notice_attempts
                WHERE intent_id=? ORDER BY attempt""", (row["id"],)).fetchall()]
        return result

    @staticmethod
    def _executions_for_event(db, event_id):
        rows = db.execute("""SELECT id, plan_id, action_index, action, mode, status, attempts, duration_seconds,
            expires_at, platform_code, reason_code, trace_id, updated_at FROM execution_intents
            WHERE source_event_id=? ORDER BY action_index""", (event_id,)).fetchall()
        return [DryRunQueue._execution_details(db, row) for row in rows]

    @staticmethod
    def _execution_for_event(db, event_id):
        executions = DryRunQueue._executions_for_event(db, event_id)
        return executions[0] if executions else None

    @staticmethod
    def _execution_summary(executions):
        states = [item["status"] for item in executions]
        if not states:
            return None
        if "pending_confirm" in states:
            return "pending_confirm"
        if any(state in ("pending_auto", "pending_retry", "executing") for state in states):
            return "executing"
        if any(state in ("unknown", "review") for state in states):
            return "unknown"
        if all(state == "succeeded" for state in states):
            return "succeeded"
        if all(state == "failed" for state in states):
            return "failed"
        if all(state in ("blocked", "permission_denied", "denied", "unsupported") for state in states):
            return "blocked"
        if all(state == "inactive" for state in states):
            return "inactive"
        return "partial_failed"

    def message_at_event(self, platform_id, group_id, message_id, event_id):
        db = self._connect()
        try:
            rows = db.execute("""SELECT id, status, detail, attempt_id, created_at FROM dry_run_events
                WHERE platform_id=? AND group_id=? AND message_id=? AND id<=? ORDER BY id""",
                (platform_id, group_id, message_id, event_id)).fetchall()
            if not rows or rows[-1]["id"] != event_id:
                return None
            latest = dict(rows[-1])
            latest.update(platform_id=platform_id, group_id=group_id, message_id=message_id, first_created_at=rows[0]["created_at"],
                          events=[{key: row[key] for key in ("id", "status", "created_at")} for row in rows])
            latest["executions"] = self._executions_for_event(db, event_id)
            latest["execution"] = latest["executions"][0] if latest["executions"] else None
            latest["execution_summary"] = self._execution_summary(latest["executions"])
            return latest
        finally:
            db.close()

    def list_events(self, *, platform_id: str, group_id: str,
                    after_id: int = 0, limit: int = 50) -> list[dict]:
        """Read one group page. Authorization belongs to the calling API."""
        if any(not isinstance(x, str) or not x.strip() for x in (platform_id, group_id)):
            raise ValueError("invalid group identity")
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid pagination")
        db = self._connect()
        try:
            rows = db.execute(
                """SELECT id, message_id, status, detail, attempt_id, created_at FROM dry_run_events
                   WHERE platform_id=? AND group_id=? AND id>?
                   ORDER BY id LIMIT ?""", (platform_id, group_id, after_id, limit)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            db.close()

    def message_content(self, *, platform_id: str, group_id: str, message_id: str):
        if any(not isinstance(value, str) or not value.strip() or len(value) > 512
               for value in (platform_id, group_id, message_id)):
            raise ValueError("invalid message identity")
        from .message_retention import stored_retention_days
        db = self._connect()
        try:
            db.execute("BEGIN")
            days = stored_retention_days(db)
            if not days:
                return None
            row = db.execute("""SELECT content FROM message_contents
                WHERE platform_id=? AND group_id=? AND message_id=?
                  AND stored_at > strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)""",
                             (platform_id, group_id, message_id, f"-{days} days")).fetchone()
            return row["content"] if row else None
        finally:
            db.close()

    def pending_confirmations(self, platform_id, group_id, before_id=None, limit=20, all_groups=False):
        """Read pending supported actions and counts from one consistent snapshot."""
        if type(all_groups) is not bool or (all_groups and (platform_id is not None or group_id is not None)):
            raise ValueError("invalid group identity")
        if not all_groups and any(not isinstance(value, str) or not value.strip() or len(value) > 128
                                  for value in (platform_id, group_id)):
            raise ValueError("invalid group identity")
        if (before_id is not None and (type(before_id) is not int or not 1 <= before_id <= 9223372036854775807)
                or type(limit) is not int or not 1 <= limit <= 100):
            raise ValueError("invalid pagination")
        from .message_retention import stored_retention_days
        db = self._connect()
        try:
            db.execute("BEGIN")
            predicate = "platform_id != ?" if all_groups else "platform_id=? AND group_id=?"
            scope = (SYSTEM_PLATFORM,) if all_groups else (platform_id, group_id)
            # Only actions supported by this plugin's real execution boundary
            # belong in the executable confirmation queue.
            where = f"status='pending_confirm' AND mode='confirm' AND action='mute' AND {predicate}"
            totals = db.execute(f"""SELECT COUNT(*) AS messages, COALESCE(SUM(actions), 0) AS actions FROM (
                SELECT COUNT(*) AS actions FROM execution_intents WHERE {where}
                GROUP BY platform_id, group_id, message_id)""", scope).fetchone()
            rows = db.execute(f"""SELECT platform_id, group_id, message_id, MAX(id) AS cursor
                FROM execution_intents WHERE {where} GROUP BY platform_id, group_id, message_id
                HAVING (? IS NULL OR MAX(id) < ?) ORDER BY cursor DESC LIMIT ?""",
                (*scope, before_id, before_id, limit + 1)).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            days = stored_retention_days(db)
            messages = []
            for row in rows:
                key = (row["platform_id"], row["group_id"], row["message_id"])
                actions = [dict(item) for item in db.execute("""SELECT i.id, i.action, i.mode, i.status,
                    i.rule_id, i.rule_version, i.duration_seconds, i.expires_at, i.message_time,
                    i.action_index, e.detail, e.created_at FROM execution_intents i
                    JOIN dry_run_events e ON e.id=i.source_event_id
                    WHERE i.platform_id=? AND i.group_id=? AND i.message_id=?
                      AND i.status='pending_confirm' AND i.mode='confirm' AND i.action='mute'
                    ORDER BY i.source_event_id DESC, i.action_index""", key).fetchall()]
                content = None
                if days:
                    content_row = db.execute("""SELECT content FROM message_contents
                        WHERE platform_id=? AND group_id=? AND message_id=?
                        AND stored_at > strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)""",
                        (*key, f"-{days} days")).fetchone()
                    content = content_row["content"] if content_row else None
                messages.append({"platform_id": key[0], "group_id": key[1], "message_id": key[2],
                                 "content": content[:400] if content is not None else None,
                                 "content_truncated": content is not None and len(content) > 400,
                                 "cursor": row["cursor"], "actions": actions})
            return {"messages": messages, "total_messages": totals["messages"], "total_actions": totals["actions"],
                    "next_before_id": rows[-1]["cursor"] if has_more and rows else None}
        finally:
            db.close()

    def confirmation_state(self, intent_id):
        if type(intent_id) is not int or intent_id <= 0:
            raise ValueError("invalid intent")
        db = self._connect()
        try:
            row = db.execute("SELECT status, reason_code FROM execution_intents WHERE id=? AND action='mute'",
                             (intent_id,)).fetchone()
            return dict(row) if row else None
        finally:
            db.close()

    def purge_message_contents(self):
        from .message_retention import purge_expired_contents, stored_retention_days
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            days = stored_retention_days(db)
            count = purge_expired_contents(db, days) if days is not None else 0
            db.commit()
            return count
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def list_messages(self, *, platform_id: str | None, group_id: str | None,
                      before_id: int | None = None, limit: int = 50, all_groups: bool = False) -> list[dict]:
        """Page newest first; before_id excludes that latest event id and newer."""
        if type(all_groups) is not bool or (all_groups and (platform_id is not None or group_id is not None)):
            raise ValueError("invalid group identity")
        if not all_groups and any(not isinstance(x, str) or not x.strip() for x in (platform_id, group_id)):
            raise ValueError("invalid group identity")
        if (before_id is not None and (type(before_id) is not int or not 1 <= before_id <= 9223372036854775807)) or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid pagination")
        db = self._connect()
        try:
            db.execute("BEGIN")
            predicate = "platform_id != ?" if all_groups else "platform_id=? AND group_id=?"
            scope = (SYSTEM_PLATFORM,) if all_groups else (platform_id, group_id)
            rows = db.execute(f"""WITH grouped AS (
                SELECT platform_id, group_id, message_id, MIN(id) AS first_id, MAX(id) AS latest_id
                FROM dry_run_events WHERE {predicate}
                GROUP BY platform_id, group_id, message_id HAVING (? IS NULL OR MAX(id)<?) ORDER BY latest_id DESC LIMIT ?
            ) SELECT g.platform_id, g.group_id, g.message_id, e.id, e.status, f.created_at AS first_created_at,
                      e.created_at, e.detail, e.attempt_id
              FROM grouped g JOIN dry_run_events e ON e.id=g.latest_id
              JOIN dry_run_events f ON f.id=g.first_id ORDER BY e.id DESC""",
                              (*scope, before_id, before_id, limit)).fetchall()
            result = [dict(row) for row in rows]
            if result:
                by_message = {(row["platform_id"], row["group_id"], row["message_id"]): row for row in result}
                for row in result:
                    row["events"] = []
                    row["executions"] = self._executions_for_event(db, row["id"])
                    row["execution"] = row["executions"][0] if row["executions"] else None
                    row["execution_summary"] = self._execution_summary(row["executions"])
                placeholders = ",".join("(?,?,?)" for _ in result)
                timeline = db.execute(
                    "SELECT platform_id, group_id, message_id, id, status, created_at FROM dry_run_events "
                    f"WHERE (platform_id, group_id, message_id) IN ({placeholders}) ORDER BY id",
                    tuple(value for row in result for value in (row["platform_id"], row["group_id"], row["message_id"])))
                for event in timeline:
                    by_message[(event["platform_id"], event["group_id"], event["message_id"])]["events"].append(
                        {"id": event["id"], "status": event["status"], "created_at": event["created_at"]})
            return result
        finally:
            db.close()

    def list_known_groups(self) -> list[dict]:
        """List known groups from rule history, audit activity and names."""
        db = self._connect()
        try:
            rows = db.execute("""
                WITH rule_groups AS (
                    SELECT platform_id, group_id, COUNT(DISTINCT rule_id) AS rule_count
                    FROM rule_versions WHERE platform_id != ? GROUP BY platform_id, group_id
                ), audit_groups AS (
                    SELECT platform_id, group_id, MAX(created_at) AS last_activity
                    FROM dry_run_events WHERE platform_id != ? GROUP BY platform_id, group_id
                ), groups AS (
                    SELECT platform_id, group_id FROM rule_groups
                    UNION SELECT platform_id, group_id FROM audit_groups
                    UNION SELECT platform_id, group_id FROM dashboard_group_names WHERE platform_id != ?
                )
                SELECT g.platform_id, g.group_id, COALESCE(r.rule_count, 0) AS rule_count,
                       a.last_activity, COALESCE(n.alias, '') AS alias,
                       COALESCE(n.platform_name, '') AS platform_name
                FROM groups g LEFT JOIN rule_groups r USING (platform_id, group_id)
                LEFT JOIN audit_groups a USING (platform_id, group_id)
                LEFT JOIN dashboard_group_names n USING (platform_id, group_id)
                ORDER BY a.last_activity IS NULL, a.last_activity DESC, g.platform_id, g.group_id
            """, (SYSTEM_PLATFORM, SYSTEM_PLATFORM, SYSTEM_PLATFORM)).fetchall()
            return [dict(row) for row in rows]
        finally:
            db.close()

    def save_group_name(self, platform_id: str, group_id: str, name: str, *, platform_name=False) -> dict:
        column = "platform_name" if platform_name else "alias"
        db = self._connect()
        try:
            db.execute(f"""INSERT INTO dashboard_group_names (platform_id, group_id, {column}) VALUES (?, ?, ?)
                ON CONFLICT(platform_id, group_id) DO UPDATE SET {column}=excluded.{column}""",
                       (platform_id, group_id, name))
            db.commit()
            return dict(db.execute("SELECT platform_id, group_id, alias, platform_name FROM dashboard_group_names "
                                   "WHERE platform_id=? AND group_id=?", (platform_id, group_id)).fetchone())
        finally:
            db.close()

    def ensure_group(self, platform_id: str, group_id: str, platform_name: str = "") -> dict:
        db = self._connect()
        try:
            db.execute("INSERT OR IGNORE INTO dashboard_group_names (platform_id, group_id, platform_name) VALUES (?, ?, ?)",
                       (platform_id, group_id, platform_name))
            db.commit()
            return dict(db.execute("SELECT platform_id, group_id, alias, platform_name FROM dashboard_group_names "
                                   "WHERE platform_id=? AND group_id=?", (platform_id, group_id)).fetchone())
        finally:
            db.close()

    def summarize_group(self, *, platform_id: str | None, group_id: str | None, all_groups: bool = False) -> dict:
        """Summarize the latest event per message, with bounded streaming of details."""
        import math

        if type(all_groups) is not bool or (all_groups and (platform_id is not None or group_id is not None)):
            raise ValueError("invalid group identity")
        if not all_groups and any(not isinstance(value, str) or not value.strip() for value in (platform_id, group_id)):
            raise ValueError("invalid group identity")
        db = self._connect()
        try:
            predicate = "platform_id != ?" if all_groups else "platform_id=? AND group_id=?"
            scope = (SYSTEM_PLATFORM,) if all_groups else (platform_id, group_id)
            cursor = db.execute(f"""
                WITH latest AS (
                    SELECT MAX(id) AS id FROM dry_run_events
                    WHERE {predicate} GROUP BY platform_id, group_id, message_id
                    ORDER BY id DESC LIMIT ?
                )
                SELECT e.status, e.detail, julianday(e.created_at) >= julianday('now', '-1 day') AS recent
                FROM latest JOIN dry_run_events e ON e.id=latest.id ORDER BY e.id DESC
            """, (*scope, SUMMARY_LIMIT + 1))
            counts, recent, latencies = {}, {"messages": 0, "proposed": 0, "no_action": 0, "failed": 0, "review": 0}, []
            total = 0
            truncated = False
            for row in cursor:
                if total == SUMMARY_LIMIT:
                    truncated = True
                    break
                total += 1
                status = row["status"]
                counts[status] = counts.get(status, 0) + 1
                if not row["recent"]:
                    continue
                recent["messages"] += 1
                if status in ("proposed", "no_action"):
                    recent[status] += 1
                elif status in ("failed", "timed_out"):
                    recent["failed"] += 1
                elif status == "review":
                    recent["review"] += 1
                try:
                    detail = json.loads(row["detail"])
                    if isinstance(detail, dict) and isinstance(detail.get("decision"), dict):
                        detail = detail["decision"]
                    latency = detail.get("model_latency_ms") if isinstance(detail, dict) else None
                    if type(latency) in (int, float) and latency >= 0 and math.isfinite(latency):
                        latencies.append(latency)
                except (ValueError, TypeError, RecursionError, OverflowError):
                    pass
            latencies.sort()
            return {"total_messages": total, "status_counts": counts, "recent_24h": recent,
                    "latency_p95_ms": latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None,
                    "truncated": truncated, "limit": SUMMARY_LIMIT}
        finally:
            db.close()

    def get_ui_state(self, username: str) -> dict | None:
        db = self._connect()
        try:
            row = db.execute("SELECT platform_id, group_id, active_tab FROM dashboard_ui_state WHERE username=?",
                             (username,)).fetchone()
            return dict(row) if row else None
        finally:
            db.close()

    def get_overview_layout(self, username):
        db = self._connect()
        try:
            row = db.execute("SELECT payload FROM dashboard_overview_layout WHERE username=?", (username,)).fetchone()
            if row is None:
                return None
            return json.loads(row["payload"])
        finally:
            db.close()

    def save_overview_layout(self, username, layout):
        payload = json.dumps(layout, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        db = self._connect()
        try:
            db.execute("""INSERT INTO dashboard_overview_layout (username, payload) VALUES (?, ?)
                ON CONFLICT(username) DO UPDATE SET payload=excluded.payload""", (username, payload))
            db.commit()
            return layout
        finally:
            db.close()

    def quick_start_completed(self) -> bool:
        db = self._connect()
        try:
            return db.execute("SELECT 1 FROM dashboard_quick_start WHERE id=1").fetchone() is not None
        finally:
            db.close()

    def complete_quick_start(self) -> None:
        db = self._connect()
        try:
            db.execute("INSERT OR IGNORE INTO dashboard_quick_start (id) VALUES (1)")
            db.commit()
        finally:
            db.close()

    def save_ui_state(self, username: str, platform_id: str, group_id: str) -> dict:
        db = self._connect()
        try:
            db.execute("""INSERT INTO dashboard_ui_state (username, platform_id, group_id) VALUES (?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET platform_id=excluded.platform_id, group_id=excluded.group_id""",
                (username, platform_id, group_id))
            db.commit()
            return {"platform_id": platform_id, "group_id": group_id}
        finally:
            db.close()

    def save_active_tab(self, username: str, active_tab: str) -> dict:
        db = self._connect()
        try:
            db.execute("""INSERT INTO dashboard_ui_state (username, platform_id, group_id, active_tab)
                VALUES (?, '', '', ?) ON CONFLICT(username) DO UPDATE SET active_tab=excluded.active_tab""",
                (username, active_tab))
            db.commit()
            return {"active_tab": active_tab}
        finally:
            db.close()

    def pending_groups(self) -> list[tuple[str, str]]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT DISTINCT platform_id, group_id FROM dry_run_queue WHERE state='pending'"
            ).fetchall()
            return [(row["platform_id"], row["group_id"]) for row in rows]
        finally:
            db.close()

    def record_notification(self, code: str, status: str) -> None:
        if code not in {"model_failed", "model_timeout", "context_limit", "rule_snapshot_too_large", "queue_full", "storage_failed", "circuit_open"}:
            raise ValueError("invalid notification code")
        if status not in {"submitted", "failed", "unknown"}:
            raise ValueError("invalid notification status")
        db = self._connect()
        try:
            db.execute("INSERT INTO notification_events (code, status) VALUES (?, ?)", (code, status))
            db.commit()
        finally:
            db.close()

    def list_notifications(self, *, after_id: int = 0, limit: int = 50) -> list[dict]:
        if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid pagination")
        db = self._connect()
        try:
            rows = db.execute("SELECT id, code, status, created_at FROM notification_events WHERE id>? ORDER BY id LIMIT ?",
                              (after_id, limit)).fetchall()
            return [dict(row) for row in rows]
        finally:
            db.close()
