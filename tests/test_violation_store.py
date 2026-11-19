from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from sentinel.dry_run_queue import DryRunQueue
from sentinel.violation_store import (
    AccumulationPolicy, ViolationCount, record_violation, revoke_violation, utc_microseconds,
)


START = datetime(2026, 11, 18, tzinfo=timezone.utc)
EPSILON = timedelta(microseconds=1)


class ViolationStoreTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.queue = DryRunQueue(Path(folder.name) / "sentinel.sqlite3")
        self.queue.initialize()
        self.policy = AccumulationPolicy(START, 60, (10, 30, 45))

    def judgment(self, message, *, platform="bot", group="group", status="proposed", admitted_at=START,
                 sender="member", rule="rule"):
        db = self.queue._connect()
        try:
            with db:
                db.execute("""INSERT INTO dry_run_queue
                    (platform_id, group_id, message_id, state, admitted_at_us) VALUES (?, ?, ?, 'done', ?)
                    ON CONFLICT(platform_id, group_id, message_id) DO NOTHING""",
                    (platform, group, message, None if admitted_at is None else utc_microseconds(admitted_at)))
                return db.execute("""INSERT INTO dry_run_events
                    (platform_id, group_id, message_id, status, detail) VALUES (?, ?, ?, ?, ?)""",
                    (platform, group, message, status,
                     json.dumps({"proposal": {"rule_id": rule, "target_sender_id": sender}}))).lastrowid
        finally:
            db.close()

    def count(self, message, *, platform="bot", group="group", sender="member", rule="rule",
              event=None, admitted_at=None, counted_at=START, policy=None, max_duration_seconds=45,
              db=None):
        if event is None:
            event = self.judgment(message, platform=platform, group=group, sender=sender, rule=rule,
                                  admitted_at=counted_at if admitted_at is None else admitted_at)
        args = dict(platform_id=platform, group_id=group, message_id=message, sender_id=sender, rule_id=rule,
                    source_event_id=event, counted_at=counted_at, policy=policy or self.policy,
                    max_duration_seconds=max_duration_seconds)
        if db is not None:
            return record_violation(db, **args)
        return self.write(record_violation, **args)

    def write(self, operation, **args):
        db = self.queue._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                return operation(db, **args)
        finally:
            db.close()

    def rows(self):
        db = self.queue._connect()
        try:
            return db.execute("SELECT * FROM violation_counts ORDER BY source_event_id").fetchall()
        finally:
            db.close()

    def test_activation_is_inclusive_without_backfilling(self):
        old = self.judgment("old", admitted_at=None)
        self.queue.initialize()
        self.assertEqual(self.rows(), [])
        self.assertIsNone(self.count("old", event=old))
        self.assertIsNone(self.count("queued-before", admitted_at=START - EPSILON))
        self.assertEqual(self.count("new"), ViolationCount(1, 10))
        self.assertEqual(self.count("later"), ViolationCount(2, 30))

    def test_rolling_window_excludes_the_exact_lower_bound(self):
        self.assertEqual(self.count("first"), ViolationCount(1, 10))
        self.assertEqual(self.count("second", counted_at=START + EPSILON), ViolationCount(2, 30))
        self.assertEqual(self.count("third", counted_at=START + timedelta(seconds=60)), ViolationCount(2, 30))
        self.assertEqual(len(self.rows()), 3)

    def test_policy_reconfiguration_uses_window_without_changing_rule_identity(self):
        self.assertEqual(self.count("one"), ViolationCount(1, 10))
        updated = AccumulationPolicy(START, 90, (5, 20))
        adjusted_time = START + timedelta(seconds=61)
        self.assertEqual(self.count("two", policy=updated, counted_at=adjusted_time), ViolationCount(2, 20))
        self.assertEqual(self.count("three", policy=updated, counted_at=adjusted_time), ViolationCount(3, 20))
        new_activation = AccumulationPolicy(adjusted_time, 90, (5, 20))
        self.assertEqual(self.count("four", policy=new_activation, counted_at=adjusted_time), ViolationCount(3, 20))
        self.assertEqual(self.rows()[0]["duration_seconds"], 10)

    def test_platform_group_member_and_rule_isolation(self):
        self.assertEqual(self.count("one"), ViolationCount(1, 10))
        for message, scope in [
            ("one", {"platform": "bot2"}), ("one", {"group": "group2"}),
            ("other-member", {"sender": "member2"}), ("other-rule", {"rule": "rule2"}),
        ]:
            with self.subTest(scope=scope):
                self.assertEqual(self.count(message, **scope), ViolationCount(1, 10))
        self.assertIsNone(self.count("one", rule="rule2"))
        self.assertEqual(self.count("two"), ViolationCount(2, 30))

    def test_revoke_keeps_the_original_audit_and_prevents_recount(self):
        first = self.count("one")
        args = dict(platform_id="bot", group_id="group", message_id="one",
                    revoked_at=START + EPSILON, revoked_by="dashboard-admin")
        self.assertFalse(self.write(revoke_violation, **(args | {"group_id": "other"})))
        self.assertTrue(self.write(revoke_violation, **args))
        self.assertFalse(self.write(revoke_violation, **(args | {"revoked_by": "another-admin"})))
        self.assertIsNone(self.count("one", counted_at=START + EPSILON))
        self.assertEqual(self.count("two", counted_at=START + EPSILON), first)
        self.assertEqual(self.rows()[0]["revoked_by"], "dashboard-admin")
        self.assertEqual(self.rows()[0]["ordinal"], 1)
        self.assertEqual(self.rows()[0]["duration_seconds"], 10)

    def test_rejects_missing_mismatched_and_unaccepted_source(self):
        event = self.judgment("somewhere-else")
        for bad_event in [event, self.judgment("one", status="no_action"),
                          self.judgment("one", status="review"), 999]:
            with self.subTest(bad_event=bad_event), self.assertRaisesRegex(ValueError, "matching"):
                self.count("one", event=bad_event)
        self.assertEqual(self.rows(), [])

    def test_bound_rule_and_sender_must_match_the_judgment(self):
        event = self.judgment("one")
        with self.assertRaisesRegex(ValueError, "judgment identity"):
            self.count("one", event=event, rule="other")
        with self.assertRaisesRegex(ValueError, "judgment identity"):
            self.count("one", event=event, sender="other")
        self.assertEqual(self.rows(), [])

    def test_invalid_tiers_and_active_mute_limit_fail_closed(self):
        for window, tiers in [(0, (1,)), (True, (1,)), (60, ()), (60, (True,)),
                              (60, (2, 1)), (60, (2_592_001,)), (60, [1])]:
            with self.subTest(window=window, tiers=tiers), self.assertRaises(ValueError):
                AccumulationPolicy(START, window, tiers)
        with self.assertRaisesRegex(ValueError, "active mute limit"):
            self.count("one", max_duration_seconds=44)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.count("one"), ViolationCount(1, 10))

    def test_times_are_timezone_aware_and_ordered(self):
        event = self.judgment("one", admitted_at=START + EPSILON)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.count("one", event=event, counted_at=START.replace(tzinfo=None))
        with self.assertRaisesRegex(ValueError, "before message admission"):
            self.count("one", event=event)
        same_instant = START.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(self.count("two", admitted_at=same_instant), ViolationCount(1, 10))

    def test_schema_upgrade_and_new_queue_admission(self):
        queue = DryRunQueue(Path(self.queue.path).with_name("legacy.sqlite3"))
        db = queue._connect()
        try:
            with db:
                db.execute("""CREATE TABLE dry_run_queue (
                    id INTEGER PRIMARY KEY, platform_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, payload TEXT, state TEXT NOT NULL, lease TEXT,
                    UNIQUE(platform_id, group_id, message_id))""")
                db.execute("INSERT INTO dry_run_queue VALUES (1, 'bot', 'group', 'old', NULL, 'done', NULL)")
            queue.initialize()
            before = utc_microseconds(datetime.now(timezone.utc))
            self.assertEqual(queue.enqueue(platform_id="bot", group_id="group", message_id="new",
                                           payload={"admitted_at_us": 0}, capacity=1), "queued")
            after = utc_microseconds(datetime.now(timezone.utc))
            rows = db.execute("SELECT admitted_at_us FROM dry_run_queue ORDER BY id").fetchall()
            self.assertIsNone(rows[0][0])
            self.assertLessEqual(before, rows[1][0])
            self.assertLessEqual(rows[1][0], after)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM violation_counts").fetchone()[0], 0)
        finally:
            db.close()

    def test_restart_and_expiry_do_not_allow_duplicate_messages(self):
        self.assertEqual(self.count("one"), ViolationCount(1, 10))
        self.queue = DryRunQueue(self.queue.path)
        self.queue.initialize()
        self.assertEqual(self.count("two"), ViolationCount(2, 30))
        much_later = START + timedelta(days=30)
        self.assertIsNone(self.count("one", counted_at=much_later))
        self.assertEqual(self.count("three", counted_at=much_later), ViolationCount(1, 10))

    def test_failed_caller_transaction_rolls_back_count(self):
        event = self.judgment("one")
        db = self.queue._connect()
        try:
            with self.assertRaisesRegex(RuntimeError, "existing write transaction"):
                self.count("one", event=event, db=db)
            with self.assertRaises(sqlite3.OperationalError), db:
                db.execute("BEGIN IMMEDIATE")
                self.assertEqual(self.count("one", event=event, db=db), ViolationCount(1, 10))
                db.execute("INSERT INTO missing_execution_intent_table VALUES (1)")
        finally:
            db.close()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.count("one", event=event), ViolationCount(1, 10))

    def test_concurrent_writes_keep_distinct_ordinals_and_deduplicate(self):
        messages = ("one", "one", "two", "three", "four")
        barrier = Barrier(len(messages), timeout=10)

        def worker(message):
            barrier.wait()
            return self.count(message)

        with ThreadPoolExecutor(max_workers=len(messages)) as executor:
            results = list(executor.map(worker, messages))
        self.assertEqual(results.count(None), 1)
        self.assertEqual(sorted((result.ordinal, result.duration_seconds) for result in results if result),
                         [(1, 10), (2, 30), (3, 45), (4, 45)])
