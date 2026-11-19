"""Transactional violation counts, independent of platform action outcomes.

Callers supply trusted identities, times and policy, after validating the
decision or existing manual confirmation. The ledger never inspects a model
response and never calls a platform.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3


MAX_MUTE_SECONDS = 2_592_000
MAX_WINDOW_SECONDS = 365 * 86400
MAX_TIERS = 30
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECONDS = 1_000_000


def utc_microseconds(value: datetime) -> int:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("expected a timezone-aware timestamp")
    delta = value.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * _MICROSECONDS + delta.microseconds


def _require_identity(*values: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("missing violation identity")


def validate_ladder(window_seconds, tiers):
    if type(window_seconds) is not int or not 0 < window_seconds <= MAX_WINDOW_SECONDS:
        raise ValueError("invalid violation window")
    if (not isinstance(tiers, tuple) or not 1 <= len(tiers) <= MAX_TIERS
            or any(type(tier) is not int or not 0 < tier <= MAX_MUTE_SECONDS for tier in tiers)
            or any(previous > current for previous, current in zip(tiers, tiers[1:]))):
        raise ValueError("invalid violation tiers")


@dataclass(frozen=True)
class AccumulationPolicy:
    enabled_at: datetime
    window_seconds: int
    tiers_seconds: tuple[int, ...]

    def __post_init__(self):
        utc_microseconds(self.enabled_at)
        validate_ladder(self.window_seconds, self.tiers_seconds)

    def duration_for(self, ordinal: int, *, max_duration_seconds: int) -> int:
        if type(ordinal) is not int or ordinal <= 0:
            raise ValueError("invalid violation ordinal")
        if (type(max_duration_seconds) is not int
                or not self.tiers_seconds[-1] <= max_duration_seconds <= MAX_MUTE_SECONDS):
            raise ValueError("violation tiers exceed the active mute limit")
        return self.tiers_seconds[min(ordinal, len(self.tiers_seconds)) - 1]


@dataclass(frozen=True)
class ViolationCount:
    ordinal: int
    duration_seconds: int


def active_policy(db, rule, mute):
    """Find the start of the rule's current uninterrupted cumulative policy."""
    enabled_at = rule.created_at
    rows = db.execute("""SELECT duration_mode, created_at FROM rule_versions
        WHERE platform_id=? AND group_id=? AND rule_id=? AND version<?
        ORDER BY version DESC""", (rule.platform_id, rule.group_id, rule.rule_id, rule.version))
    for row in rows:
        if row["duration_mode"] != "cumulative":
            break
        enabled_at = row["created_at"]
    return AccumulationPolicy(datetime.fromisoformat(enabled_at.replace("Z", "+00:00")),
                              mute["window_seconds"], tuple(mute["tiers_seconds"]))


def count_cumulative_mute(db, intent, rule, mute, group_settings, now):
    # Keep the calculated tier for auditing even if the global limit has since
    # been reduced. The execution path separately enforces the active limit.
    shared = next((item["rule_ids"] for item in group_settings["shared_violation_groups"]
                   if rule.rule_id in item["rule_ids"]), ())
    return record_violation(
        db, platform_id=intent["platform_id"], group_id=intent["group_id"],
        message_id=intent["message_id"], sender_id=intent["target_sender_id"],
        rule_id=rule.rule_id, source_event_id=intent["source_event_id"], counted_at=now,
        policy=active_policy(db, rule, mute), max_duration_seconds=MAX_MUTE_SECONDS,
        shared_rules=tuple(shared),
    )


def initialize_violation_store(db: sqlite3.Connection) -> None:
    """Create empty storage on upgrade; never backfill historical decisions."""
    db.execute("""CREATE TABLE IF NOT EXISTS violation_counts (
        platform_id TEXT NOT NULL, group_id TEXT NOT NULL, message_id TEXT NOT NULL,
        sender_id TEXT NOT NULL, rule_id TEXT NOT NULL,
        source_event_id INTEGER NOT NULL UNIQUE,
        admitted_at_us INTEGER NOT NULL, counted_at_us INTEGER NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal > 0),
        duration_seconds INTEGER NOT NULL CHECK(duration_seconds BETWEEN 1 AND 2592000),
        revoked_at_us INTEGER, revoked_by TEXT,
        CHECK(admitted_at_us <= counted_at_us),
        CHECK((revoked_at_us IS NULL AND revoked_by IS NULL) OR
              (revoked_at_us IS NOT NULL AND revoked_at_us >= counted_at_us AND revoked_by IS NOT NULL)),
        PRIMARY KEY(platform_id, group_id, message_id)
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS violation_counts_active_window
        ON violation_counts(platform_id, group_id, sender_id, rule_id, counted_at_us)
        WHERE revoked_at_us IS NULL""")


def _require_transaction(db: sqlite3.Connection) -> None:
    if not db.in_transaction:
        raise RuntimeError("violation writes require an existing write transaction")


def record_violation(
    db: sqlite3.Connection, *,
    platform_id: str, group_id: str, message_id: str, sender_id: str, rule_id: str,
    source_event_id: int, counted_at: datetime,
    policy: AccumulationPolicy, max_duration_seconds: int, shared_rules: tuple[str, ...] = (),
) -> ViolationCount | None:
    """Count an accepted violation in the caller's BEGIN IMMEDIATE transaction.

    An old message or an already recorded message (even if revoked) returns
    None and must not produce a new cumulative mute. The caller owns commit and
    rollback, so a new count and its execution intent can be saved together.
    Admission time is read from the queue, not from the model or message.
    Choose `counted_at` inside the transaction when the violation becomes
    effective, not when the mute result arrives.
    """
    _require_transaction(db)
    _require_identity(platform_id, group_id, message_id, sender_id, rule_id)
    if type(source_event_id) is not int or source_event_id <= 0:
        raise ValueError("invalid violation source event")
    counted = utc_microseconds(counted_at)
    starts = utc_microseconds(policy.enabled_at)

    key = (platform_id, group_id, message_id)
    if db.execute("""SELECT 1 FROM violation_counts
        WHERE platform_id=? AND group_id=? AND message_id=?""", key).fetchone():
        return None
    source = db.execute("""SELECT q.admitted_at_us, e.detail FROM dry_run_events AS e JOIN dry_run_queue AS q
        USING (platform_id, group_id, message_id)
        WHERE e.id=? AND e.platform_id=? AND e.group_id=? AND e.message_id=?
            AND e.status='proposed' AND q.state='done'""",
        (source_event_id, *key)).fetchone()
    if source is None:
        raise ValueError("missing matching violation judgment")
    admitted, detail = source
    if admitted is None or admitted < starts:
        return None
    if admitted > counted:
        raise ValueError("cannot count before message admission")
    try:
        proposal = json.loads(detail)["proposal"]
        if proposal["rule_id"] != rule_id or proposal["target_sender_id"] != sender_id:
            raise ValueError("violation identity mismatch")
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise ValueError("invalid violation judgment identity") from error

    rules = shared_rules if rule_id in shared_rules else (rule_id,)
    placeholders = ", ".join("?" for _ in rules)
    # Past shared entries were themselves activated and validated when counted.
    # The current rule's previous activation is always excluded for that rule.
    lower_bound = counted - policy.window_seconds * _MICROSECONDS
    previous = db.execute(f"""SELECT COUNT(*) FROM violation_counts
        WHERE platform_id=? AND group_id=? AND sender_id=? AND rule_id IN ({placeholders})
            AND revoked_at_us IS NULL AND (rule_id != ? OR admitted_at_us >= ?)
            AND counted_at_us > ? AND counted_at_us <= ?""",
        (platform_id, group_id, sender_id, *rules, rule_id, starts, lower_bound, counted)).fetchone()[0]
    ordinal = previous + 1
    result = ViolationCount(ordinal, policy.duration_for(ordinal, max_duration_seconds=max_duration_seconds))
    db.execute("""INSERT INTO violation_counts
        (platform_id, group_id, message_id, sender_id, rule_id, source_event_id, admitted_at_us,
         counted_at_us, ordinal, duration_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (*key, sender_id, rule_id, source_event_id, admitted, counted, ordinal, result.duration_seconds))
    return result


def revoke_violation(
    db: sqlite3.Connection, *,
    platform_id: str, group_id: str, message_id: str, revoked_at: datetime, revoked_by: str,
) -> bool:
    """Revoke once, retaining the original count and the first operator.

    The caller must authorize the operator. No execution intent is modified.
    """
    _require_transaction(db)
    _require_identity(platform_id, group_id, message_id, revoked_by)
    revoked = utc_microseconds(revoked_at)
    result = db.execute("""UPDATE violation_counts SET revoked_at_us=?, revoked_by=?
        WHERE platform_id=? AND group_id=? AND message_id=? AND revoked_at_us IS NULL""",
        (revoked, revoked_by, platform_id, group_id, message_id))
    return result.rowcount == 1
