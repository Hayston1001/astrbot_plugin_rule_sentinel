"""Bounded retention of message text in the local plugin database."""

import json


MAX_RETENTION_DAYS = 36500


def validate_retention_days(value):
    if type(value) is not int or not 0 <= value <= MAX_RETENTION_DAYS:
        raise ValueError("invalid message retention")
    return value


def stored_retention_days(db):
    table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dashboard_settings'").fetchone()
    if not table:
        return None
    row = db.execute("SELECT payload FROM dashboard_settings WHERE id=1").fetchone()
    if not row:
        return None
    config = json.loads(row[0])
    if not isinstance(config, dict):
        raise ValueError("invalid settings")
    return validate_retention_days(config.get("message_retention_days"))


def purge_expired_contents(db, days):
    validate_retention_days(days)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    count = 0
    if "message_contents" in tables:
        if days == 0:
            count = db.execute("DELETE FROM message_contents").rowcount
        else:
            count = db.execute("DELETE FROM message_contents WHERE stored_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
                               (f"-{days} days",)).rowcount
    if "message_previews" in tables:
        if days == 0:
            db.execute("DELETE FROM message_previews")
        else:
            db.execute("DELETE FROM message_previews WHERE stored_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
                       (f"-{days} days",))
    return count
