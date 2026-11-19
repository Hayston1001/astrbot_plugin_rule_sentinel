"""Transactional mute intent and attempt state."""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from .mute_validation import revalidate_mute
from .recall_validation import revalidate_recall
from .plugin_settings import read_settings
from .rule_store import RuleStore
from .action_config import resolve_action_mode, validate_actions
from .mute_validation import MuteCheck
from .group_settings import execution_allowed, resolve_group_settings
from .violation_store import count_cumulative_mute


class MuteExecutionStore:
    def __init__(self, path, notify=None):
        self.path, self.notify = str(path), notify

    def _notify(self, intent_id):
        if self.notify is not None:
            try:
                self.notify(intent_id)
            except Exception:
                pass

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _check_rule(intent, rule, max_duration_seconds, default_action_mode=None, *, cumulative_duration=None,
                    awaiting_count=False):
        if intent.get("action") == "reply":
            if any(not isinstance(intent.get(key), str) or not intent[key].strip()
                   for key in ("platform_id", "group_id", "message_id", "target_sender_id")):
                return MuteCheck(False, "invalid_message_identity")
            if (rule is None or not rule.enabled or rule.platform_id != intent.get("platform_id")
                    or rule.group_id != intent.get("group_id") or rule.rule_id != intent.get("rule_id")
                    or rule.version != intent.get("rule_version")):
                return MuteCheck(False, "rule_version_changed")
            try:
                action = next((item for item in validate_actions(rule.actions) if item["type"] == "reply"), None)
            except (ValueError, TypeError):
                return MuteCheck(False, "invalid_rule_actions")
            try:
                allowed = bool(action and resolve_action_mode(action["mode"], default_action_mode) == intent.get("mode"))
            except ValueError:
                allowed = False
            return MuteCheck(allowed, "validated" if allowed else "action_not_allowed")
        if intent.get("action") == "recall":
            return revalidate_recall(intent, rule=rule, default_action_mode=default_action_mode)
        return revalidate_mute(intent, rule=rule, max_duration_seconds=max_duration_seconds,
                               default_action_mode=default_action_mode, cumulative_duration=cumulative_duration,
                               awaiting_count=awaiting_count)

    @staticmethod
    def _counted_duration(db, intent):
        if intent["action"] != "mute":
            return None
        row = db.execute("""SELECT duration_seconds FROM violation_counts WHERE source_event_id=?
            AND platform_id=? AND group_id=? AND message_id=? AND rule_id=? AND sender_id=? AND revoked_at_us IS NULL""",
            (intent["source_event_id"], intent["platform_id"], intent["group_id"], intent["message_id"],
             intent["rule_id"], intent["target_sender_id"])).fetchone()
        return row[0] if row else None

    @staticmethod
    def _recall_expired(intent):
        created = datetime.fromisoformat(intent["message_time"])
        if created.tzinfo is None or created > datetime.now(timezone.utc):
            raise ValueError("invalid recall timestamp")
        return (datetime.now(timezone.utc) - created).total_seconds() > 110

    def begin_attempt(self, intent_id, *, requested_mode, allow_retry=False, required_action=None):
        """Atomically claim an intent and persist an attempt before any HTTP call.

        The caller must independently verify platform permissions before
        sending. A process crash after this commit is an unknown result; it must
        never lead to an automatic repeat.
        """
        if (type(intent_id) is not int or intent_id <= 0 or requested_mode not in ("auto", "confirm")
                or type(allow_retry) is not bool or required_action not in (None, "mute")):
            raise ValueError("invalid execution request")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM execution_intents WHERE id=?", (intent_id,)).fetchone()
            if row is None:
                raise ValueError("unknown intent")
            intent = dict(row)
            eligible = intent["status"] == ("pending_retry" if allow_retry else f"pending_{requested_mode}")
            if intent["mode"] != requested_mode or not eligible or (required_action and intent["action"] != required_action):
                db.commit()
                return None
            settings_row = db.execute("SELECT payload FROM dashboard_settings WHERE id=1").fetchone()
            config = json.loads(settings_row["payload"]) if settings_row else {}
            if not execution_allowed(config, intent["platform_id"], intent["group_id"]):
                db.execute("UPDATE execution_intents SET status='inactive', updated_at=? WHERE id=?",
                           (datetime.now(timezone.utc).isoformat(timespec="milliseconds"), intent_id))
                db.commit()
                self._notify(intent_id)
                return None
            settings = read_settings(config)
            key = (intent["platform_id"], intent["group_id"])
            policy = settings.policy
            cumulative = False
            if (policy.mode == "allowlist" and key not in policy.groups) or (policy.mode == "blocklist" and key in policy.groups):
                reason = "group_not_monitored"
            else:
                rule_row = db.execute("""SELECT * FROM rule_versions WHERE platform_id=? AND group_id=? AND rule_id=?
                    ORDER BY version DESC LIMIT 1""", (*key, intent["rule_id"])).fetchone()
                rule = RuleStore._decode(rule_row) if rule_row else None
                mute = next((action for action in rule.actions if action["type"] == "mute"), None) if rule else None
                cumulative = bool(intent["action"] == "mute" and mute and mute["duration_mode"] == "cumulative")
                awaiting_count = (cumulative and intent["reason_code"] == "cumulative_mute_on_confirmation"
                                  and requested_mode == "confirm" and not allow_retry and intent["attempts"] == 0)
                counted_duration = self._counted_duration(db, intent) if cumulative else None
                check = self._check_rule(intent, rule, settings.max_duration_seconds, config.get("default_action_mode"),
                                         cumulative_duration=counted_duration, awaiting_count=awaiting_count)
                if check.reason == "awaiting_count":
                    now = datetime.now(timezone.utc)
                    count = count_cumulative_mute(db, intent, rule, mute, resolve_group_settings(config, *key), now)
                    if count is None:
                        check = MuteCheck(False, "cumulative_not_counted")
                    else:
                        intent.update(duration_seconds=count.duration_seconds, created_at=now.isoformat(timespec="milliseconds"),
                                      expires_at=(now + timedelta(seconds=count.duration_seconds)).isoformat(timespec="milliseconds"))
                        db.execute("""UPDATE execution_intents SET duration_seconds=?, created_at=?, expires_at=?, reason_code=NULL
                            WHERE id=?""", (intent["duration_seconds"], intent["created_at"], intent["expires_at"], intent_id))
                        check = self._check_rule(intent, rule, settings.max_duration_seconds, config.get("default_action_mode"),
                                                 cumulative_duration=count.duration_seconds)
                reason = None if check.allowed else check.reason
            if intent["action"] != "reply":
                try:
                    expires = datetime.fromisoformat(intent["expires_at"])
                    created = datetime.fromisoformat(intent["message_time"] if intent["action"] == "recall" else intent["created_at"])
                    duration = 110 if intent["action"] == "recall" else intent["duration_seconds"]
                    if (expires.tzinfo is None or created.tzinfo is None or type(duration) is not int
                            or abs((expires - (created + timedelta(seconds=duration))).total_seconds()) >= 1):
                        reason = reason or "invalid_expiry"
                    elif intent["action"] == "recall" and self._recall_expired(intent):
                        reason = reason or "recall_expired"
                    elif intent["action"] != "recall" and expires <= datetime.now(timezone.utc):
                        reason = reason or "intent_expired"
                except (TypeError, ValueError, OverflowError):
                    reason = reason or "invalid_expiry"
            if reason:
                state = "blocked" if reason == "recall_expired" else "review"
                db.execute("UPDATE execution_intents SET status=?, reason_code=?, updated_at=? WHERE id=?",
                           (state, reason, datetime.now(timezone.utc).isoformat(timespec="milliseconds"), intent_id))
                db.commit()
                self._notify(intent_id)
                return None
            attempt = intent["attempts"] + 1
            if attempt > 3:
                raise ValueError("attempt limit exceeded")
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            db.execute("""INSERT INTO execution_attempts (intent_id, attempt, status, expires_at, created_at, updated_at)
                VALUES (?, ?, 'intent', ?, ?, ?)""", (intent_id, attempt, intent["expires_at"] or "", now, now))
            db.execute("UPDATE execution_intents SET status='executing', attempts=?, updated_at=? WHERE id=?",
                       (attempt, now, intent_id))
            db.commit()
            self._notify(intent_id)
            if intent["action"] == "reply":
                intent["template"] = next(action["template"] for action in rule.actions if action["type"] == "reply")
                previous = db.execute("SELECT reason_code FROM execution_attempts WHERE intent_id=? AND attempt=1", (intent_id,)).fetchone()
                intent["first_reason"] = previous["reason_code"] if previous else None
            return {**intent, "attempt": attempt}
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def complete_attempt(self, intent_id, attempt, *, outcome, platform_code=None, trace_id=None, reason_code=None,
                         retry_allowed=True):
        if type(intent_id) is not int or type(attempt) is not int or outcome not in ("succeeded", "failed", "unknown", "blocked", "permission_denied", "review"):
            raise ValueError("invalid execution result")
        if reason_code not in (None, "protected_target", "permission_denied", "recall_expired", "invalid_message",
                               "quote_expired", "mute_status_unavailable", "already_muted", "intent_expired",
                               "earlier_mute_unresolved", "mute_status_changed", "mute_limit_reached") or (
                                   outcome == "blocked" and reason_code not in (
                                       "protected_target", "recall_expired", "already_muted", "intent_expired",
                                       "mute_limit_reached")):
            raise ValueError("invalid execution reason")
        if platform_code is not None and (not isinstance(platform_code, str) or not re.fullmatch(r"[0-9-]{1,20}", platform_code)):
            raise ValueError("invalid platform code")
        if trace_id is not None and (not isinstance(trace_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", trace_id)):
            raise ValueError("invalid trace id")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status, attempts, action, platform_id, group_id FROM execution_intents WHERE id=?", (intent_id,)).fetchone()
            if row is None or row["status"] != "executing" or row["attempts"] != attempt:
                raise ValueError("attempt not active")
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            result = db.execute("""UPDATE execution_attempts SET status=?, platform_code=?, trace_id=?, reason_code=?, updated_at=?
                WHERE intent_id=? AND attempt=? AND status='intent'""", (outcome, platform_code, trace_id, reason_code, now, intent_id, attempt))
            if result.rowcount != 1:
                raise ValueError("attempt not recorded")
            state = "pending_retry" if retry_allowed and outcome == "failed" and reason_code != "invalid_message" and attempt < 3 else outcome
            if row["action"] == "reply" and attempt == 2:
                first = db.execute("SELECT reason_code FROM execution_attempts WHERE intent_id=? AND attempt=1", (intent_id,)).fetchone()
                if not first or first["reason_code"] != "quote_expired":
                    state = outcome
            db.execute("UPDATE execution_intents SET status=?, platform_code=?, trace_id=?, reason_code=?, updated_at=? WHERE id=?",
                       (state, platform_code, trace_id, reason_code, now, intent_id))
            if outcome == "blocked" and reason_code == "protected_target":
                setting = db.execute("SELECT payload FROM dashboard_settings WHERE id=1").fetchone()
                config = json.loads(setting["payload"]) if setting else {}
                try:
                    group = resolve_group_settings(config, row["platform_id"], row["group_id"])
                    enabled = execution_allowed(config, row["platform_id"], row["group_id"]) and group["protected_policy"] == "group_message"
                except (KeyError, TypeError, ValueError):
                    enabled = False
                if enabled:
                    db.execute("""INSERT OR IGNORE INTO protected_target_notices
                        (intent_id, status, template, quote, created_at, updated_at)
                        VALUES (?, 'pending', ?, ?, ?, ?)""", (intent_id, group["protected_template"],
                        int(group["protected_quote"]), now, now))
            db.commit()
            self._notify(intent_id)
            return state
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def recheck_attempt(self, intent_id, attempt):
        """Recheck durable policy and current rule immediately before sending."""
        db = self._connect()
        try:
            db.execute("BEGIN")
            return self._recheck_in_transaction(db, intent_id, attempt)
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            return False
        finally:
            db.close()

    @staticmethod
    def _recheck_in_transaction(db, intent_id, attempt):
        try:
            row = db.execute("SELECT * FROM execution_intents WHERE id=? AND status='executing' AND attempts=?",
                             (intent_id, attempt)).fetchone()
            setting = db.execute("SELECT payload FROM dashboard_settings WHERE id=1").fetchone()
            if row is None or setting is None:
                return False
            intent, config = dict(row), json.loads(setting["payload"])
            if not execution_allowed(config, intent["platform_id"], intent["group_id"]):
                return False
            if (MuteExecutionStore._recall_expired(intent) if intent["action"] == "recall"
                    else intent["action"] != "reply" and datetime.fromisoformat(intent["expires_at"]) <= datetime.now(timezone.utc)):
                return False
            settings = read_settings(config)
            key = (intent["platform_id"], intent["group_id"])
            policy = settings.policy
            if (policy.mode == "allowlist" and key not in policy.groups) or (policy.mode == "blocklist" and key in policy.groups):
                return False
            rule_row = db.execute("SELECT * FROM rule_versions WHERE platform_id=? AND group_id=? AND rule_id=? ORDER BY version DESC LIMIT 1",
                                  (*key, intent["rule_id"])).fetchone()
            rule = RuleStore._decode(rule_row) if rule_row else None
            return MuteExecutionStore._check_rule(intent, rule, settings.max_duration_seconds, config.get("default_action_mode"),
                                                   cumulative_duration=MuteExecutionStore._counted_duration(db, intent)).allowed
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            return False

    def acquire_send_guard(self, intent_id, attempt):
        """Keep settings and rules stable until the outbound request completes.

        A reserved SQLite write lock protects against concurrent processes too.
        The returned connection must be closed even on cancellation.
        """
        db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            if not self._recheck_in_transaction(db, intent_id, attempt):
                row = db.execute("SELECT * FROM execution_intents WHERE id=? AND status='executing' AND attempts=?", (intent_id, attempt)).fetchone()
                if row and row["action"] == "recall" and self._recall_expired(dict(row)):
                    db.close()
                    return "recall_expired"
                db.close()
                return None
            current = db.execute("""SELECT a.id, i.platform_id, i.group_id, i.target_sender_id
                FROM execution_intents i JOIN execution_attempts a ON a.intent_id=i.id
                WHERE i.id=? AND a.attempt=? AND i.action='mute'""", (intent_id, attempt)).fetchone()
            if current:
                earlier = db.execute("""SELECT 1 FROM execution_intents i JOIN execution_attempts a
                    ON a.intent_id=i.id AND a.attempt=i.attempts
                    WHERE i.action='mute' AND i.platform_id=? AND i.group_id=? AND i.target_sender_id=?
                      AND (i.status='unknown' OR (i.status='executing' AND a.id < ?)) AND i.id != ?
                      AND (julianday(a.expires_at) IS NULL OR julianday(a.expires_at) > julianday('now'))
                    LIMIT 1""", (current["platform_id"], current["group_id"], current["target_sender_id"],
                                 current["id"], intent_id)).fetchone()
                if earlier:
                    db.close()
                    return "earlier_mute_unresolved"
            return db
        except BaseException:
            db.close()
            raise

    def persist_mute_extension(self, guard, intent_id, attempt, expires_at):
        """Persist the updated deadline before QQ may apply it, then resume the guard."""
        row = guard.execute("""SELECT i.expires_at FROM execution_intents i
            JOIN execution_attempts a ON a.intent_id=i.id AND a.attempt=i.attempts
            WHERE i.id=? AND i.attempts=? AND i.action='mute' AND i.status='executing' AND a.status='intent'""",
            (intent_id, attempt)).fetchone()
        now = datetime.now(timezone.utc)
        target = datetime.fromisoformat(expires_at)
        if (row is None or target.utcoffset() is None or target <= now
                or target > now + timedelta(days=30) or target < datetime.fromisoformat(row["expires_at"])):
            raise ValueError("invalid mute extension deadline")
        updated = guard.execute("""UPDATE execution_attempts SET expires_at=?
            WHERE intent_id=? AND attempt=? AND status='intent'""", (expires_at, intent_id, attempt))
        if updated.rowcount != 1:
            raise ValueError("mute extension attempt missing")
        guard.commit()
        guard.execute("BEGIN IMMEDIATE")
        return self._recheck_in_transaction(guard, intent_id, attempt)

    @staticmethod
    def _notice_allowed(db, row):
        try:
            setting = db.execute("SELECT payload FROM dashboard_settings WHERE id=1").fetchone()
            config = json.loads(setting["payload"])
            group = resolve_group_settings(config, row["platform_id"], row["group_id"])
            policy = read_settings(config).policy
            key = (row["platform_id"], row["group_id"])
            return (execution_allowed(config, row["platform_id"], row["group_id"])
                    and ((policy.mode == "allowlist" and key in policy.groups)
                         or (policy.mode == "blocklist" and key not in policy.groups))
                    and group["protected_policy"] == "group_message"
                    and group["protected_template"] == row["template"]
                    and group["protected_quote"] == bool(row["quote"]))
        except (KeyError, TypeError, ValueError, sqlite3.Error):
            return False

    def begin_notice(self, intent_id, *, retry=False):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT n.*, i.platform_id, i.group_id, i.message_id, i.target_sender_id, i.rule_id, i.action
                FROM protected_target_notices n JOIN execution_intents i ON i.id=n.intent_id
                WHERE n.intent_id=? AND i.status='blocked' AND i.reason_code='protected_target'""", (intent_id,)).fetchone()
            if row is None or row["status"] != ("pending_retry" if retry else "pending"):
                db.commit()
                return None
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            if not self._notice_allowed(db, row):
                db.execute("UPDATE protected_target_notices SET status='inactive', updated_at=? WHERE intent_id=?", (now, intent_id))
                db.commit()
                self._notify(intent_id)
                return None
            attempt = row["attempts"] + 1
            if attempt > 3:
                raise ValueError("notice attempt limit exceeded")
            if attempt == 1:
                quoted, passive, method, downgrade_reason = int(bool(row["quote"])), int(bool(row["quote"])), "markdown", None
            elif attempt == 2 and row["reason_code"] == "quote_expired" and row["sent_passive"]:
                quoted, passive, method, downgrade_reason = int(bool(row["quote"])), 0, "markdown", "passive_expired"
            elif attempt == 2:
                quoted, passive, method, downgrade_reason = int(bool(row["quote"])), int(bool(row["quote"])), "text", "markdown_rejected"
            elif row["downgrade_reason"] == "passive_expired":
                quoted, passive, method, downgrade_reason = 0, 0, "markdown", "reference_rejected"
            else:
                raise ValueError("invalid notice retry")
            downgraded = int(downgrade_reason is not None)
            db.execute("""INSERT INTO protected_target_notice_attempts
                (intent_id, attempt, status, sent_quoted, sent_passive, send_method, downgraded, downgrade_reason, created_at, updated_at)
                VALUES (?, ?, 'intent', ?, ?, ?, ?, ?, ?, ?)""", (intent_id, attempt, quoted, passive, method, downgraded, downgrade_reason, now, now))
            db.execute("""UPDATE protected_target_notices SET status='sending', attempts=?, sent_quoted=?, sent_passive=?, send_method=?, downgraded=?, downgrade_reason=?,
                updated_at=? WHERE intent_id=?""", (attempt, quoted, passive, method, downgraded, downgrade_reason, now, intent_id))
            db.commit()
            self._notify(intent_id)
            return {**dict(row), "attempt": attempt, "sent_quoted": quoted, "sent_passive": passive, "send_method": method, "downgrade_reason": downgrade_reason}
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def acquire_notice_guard(self, intent_id, attempt):
        db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT n.*, i.platform_id, i.group_id FROM protected_target_notices n JOIN execution_intents i ON i.id=n.intent_id
                WHERE n.intent_id=? AND n.status='sending' AND n.attempts=?
                AND i.status='blocked' AND i.reason_code='protected_target'""", (intent_id, attempt)).fetchone()
            if row is None or not self._notice_allowed(db, row):
                db.close()
                return None
            return db
        except BaseException:
            db.close()
            raise

    def complete_notice(self, intent_id, attempt, *, outcome, platform_code=None, trace_id=None, reason_code=None):
        if outcome not in ("succeeded", "failed", "unknown", "inactive"):
            raise ValueError("invalid notice outcome")
        if reason_code not in (None, "quote_expired") or (reason_code and outcome != "failed"):
            raise ValueError("invalid notice reason")
        if platform_code is not None and (not isinstance(platform_code, str) or not re.fullmatch(r"[0-9-]{1,20}", platform_code)):
            raise ValueError("invalid platform code")
        if trace_id is not None and (not isinstance(trace_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", trace_id)):
            raise ValueError("invalid trace id")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            result = db.execute("""UPDATE protected_target_notice_attempts SET status=?, platform_code=?, reason_code=?, trace_id=?, updated_at=?
                WHERE intent_id=? AND attempt=? AND status='intent'""", (outcome, platform_code, reason_code, trace_id, now, intent_id, attempt))
            previous = db.execute("SELECT downgrade_reason FROM protected_target_notices WHERE intent_id=? AND status='sending' AND attempts=?", (intent_id, attempt)).fetchone()
            retryable = attempt == 1 or (attempt == 2 and previous is not None and previous["downgrade_reason"] == "passive_expired")
            state = "pending_retry" if outcome == "failed" and retryable else outcome
            updated = db.execute("""UPDATE protected_target_notices SET status=?, platform_code=?, reason_code=?, trace_id=?, updated_at=?
                WHERE intent_id=? AND status='sending' AND attempts=?""", (state, platform_code, reason_code, trace_id, now, intent_id, attempt))
            if result.rowcount != 1 or updated.rowcount != 1:
                raise ValueError("notice attempt not active")
            db.commit()
            self._notify(intent_id)
            return state
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def recover_interrupted(self):
        """Mark unresolved attempts unknown, only after exclusive ownership."""
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            db.execute("UPDATE execution_attempts SET status='unknown', updated_at=? WHERE status='intent'", (now,))
            db.execute("UPDATE execution_intents SET status='unknown', updated_at=? WHERE status='executing'", (now,))
            db.execute("UPDATE execution_intents SET status='review', updated_at=? WHERE status='pending_retry'", (now,))
            db.execute("UPDATE protected_target_notice_attempts SET status='unknown', updated_at=? WHERE status='intent'", (now,))
            db.execute("UPDATE protected_target_notices SET status='unknown', updated_at=? WHERE status='sending'", (now,))
            db.execute("UPDATE protected_target_notices SET status='review', updated_at=? WHERE status IN ('pending', 'pending_retry')", (now,))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def pending_auto(self):
        db = self._connect()
        try:
            return [row["id"] for row in db.execute("""SELECT MIN(id) AS id FROM execution_intents
                WHERE status='pending_auto' GROUP BY plan_id ORDER BY MIN(id)""")]
        finally:
            db.close()

    def plan_for_intent(self, intent_id):
        db = self._connect()
        try:
            row = db.execute("SELECT plan_id FROM execution_intents WHERE id=?", (intent_id,)).fetchone()
            if row is None:
                raise ValueError("unknown intent")
            return row["plan_id"]
        finally:
            db.close()

    def pending_plan_auto(self, plan_id):
        db = self._connect()
        try:
            return [row["id"] for row in db.execute("""SELECT id FROM execution_intents
                WHERE plan_id=? AND status='pending_auto' ORDER BY action_index""", (plan_id,))]
        finally:
            db.close()
