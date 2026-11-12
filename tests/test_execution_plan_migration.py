import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sentinel.dry_run_queue import DryRunQueue


class ExecutionPlanMigrationTests(unittest.TestCase):
    def test_legacy_intent_and_attempt_survive_and_multiple_actions_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("""CREATE TABLE execution_intents (
                    id INTEGER PRIMARY KEY, source_event_id INTEGER NOT NULL UNIQUE,
                    source_attempt_id TEXT NOT NULL, platform_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, rule_id TEXT NOT NULL, rule_version INTEGER NOT NULL,
                    action TEXT NOT NULL, target_sender_id TEXT NOT NULL, mode TEXT NOT NULL,
                    duration_seconds INTEGER, expires_at TEXT, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, platform_code TEXT, trace_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    reason_code TEXT, message_time TEXT)""")
                db.execute("""INSERT INTO execution_intents VALUES
                    (7, 12, 'lease', 'p', 'g', 'm', 'r', 2, 'mute', 's', 'auto',
                     60, 'expiry', 'succeeded', 1, '200', 'trace', 'created', 'updated', NULL, NULL)""")
                db.execute("""CREATE TABLE execution_attempts (
                    id INTEGER PRIMARY KEY, intent_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
                    status TEXT NOT NULL, expires_at TEXT NOT NULL, platform_code TEXT, trace_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, reason_code TEXT,
                    UNIQUE(intent_id, attempt))""")
                db.execute("INSERT INTO execution_attempts VALUES (1, 7, 1, 'succeeded', 'expiry', '200', 'trace', 'created', 'updated', NULL)")
            queue = DryRunQueue(path)
            queue.initialize()
            queue.initialize()
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("SELECT id, plan_id, action_index, status, attempts FROM execution_intents").fetchone(),
                                 (7, "event:12", 0, "succeeded", 1))
                self.assertEqual(db.execute("SELECT intent_id, trace_id FROM execution_attempts").fetchone(), (7, "trace"))
                db.execute("""INSERT INTO execution_intents
                    (source_event_id, source_attempt_id, platform_id, group_id, message_id,
                     rule_id, rule_version, action, target_sender_id, mode, status, created_at,
                     updated_at, plan_id, action_index)
                    VALUES (12, 'lease', 'p', 'g', 'm', 'r', 2, 'recall', 's', 'auto',
                            'pending_auto', 'created', 'updated', 'event:12', 1)""")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE execution_intents SET action_index=0 WHERE action='recall'")


if __name__ == "__main__":
    unittest.main()
