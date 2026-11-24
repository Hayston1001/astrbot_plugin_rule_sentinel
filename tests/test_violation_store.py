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
                 sender="member", rule="rule", rules=None):
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
                     json.dumps({"proposals": [{"rule_id": name, "target_sender_id": sender}
                                               for name in (rules or [rule])]}))).lastrowid
        finally:
            db.close()

    def count(self, message, *, platform="bot", group="group", sender="member", rule="rule",
              event=None, admitted_at=None, counted_at=START, policy=None, max_duration_seconds=45,
              db=None, shared=()):
        if event is None:
            event = self.judgment(message, platform=platform, group=group, sender=sender, rule=rule,
                                  admitted_at=counted_at if admitted_at is None else admitted_at)
        args = dict(platform_id=platform, group_id=group, message_id=message, sender_id=sender, rule_id=rule,
                    source_event_id=event, counted_at=counted_at, policy=policy or self.policy,
                    max_duration_seconds=max_duration_seconds, shared_rules=shared)
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
        self.assertEqual(self.count("one", rule="rule2"), ViolationCount(2, 30))
        self.assertEqual(self.count("two"), ViolationCount(2, 30))
        self.assertEqual(self.count("two", rule="rule2"), ViolationCount(3, 45))
        self.assertEqual(self.count("one", rule="rule2"), ViolationCount(2, 30))

    def test_shared_group_is_counted_once_per_message_with_one_duration(self):
        shared = ("rule", "rule2")
        event = self.judgment("one", rules=["rule", "rule2"])
        first = self.count("one", rule="rule", event=event, shared=shared)
        second = self.count("one", rule="rule2", event=event, shared=shared)
        self.assertEqual((first, second), (ViolationCount(1, 10), ViolationCount(1, 10)))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["owner"]), ["rule", "rule2"])
        self.assertEqual(self.count("one", rule="rule", event=event, shared=shared), first)
        self.assertEqual(len(self.rows()), 1)

    def test_shared_group_ladder_advances_across_member_rules_and_messages(self):
        shared = ("rule", "rule2")
        self.assertEqual(self.count("one", rule="rule", shared=shared), ViolationCount(1, 10))
        event = self.judgment("two", rules=["rule", "rule2"])
        self.assertEqual(self.count("two", rule="rule2", event=event, shared=shared), ViolationCount(2, 30))
        self.assertEqual(self.count("two", rule="rule", event=event, shared=shared), ViolationCount(2, 30))
        self.assertEqual(len(self.rows()), 2)

    def test_independent_rules_each_count_once_on_the_same_message(self):
        event = self.judgment("one", rules=["rule", "rule2"])
        self.assertEqual(self.count("one", rule="rule", event=event), ViolationCount(1, 10))
        self.assertEqual(self.count("one", rule="rule2", event=event), ViolationCount(1, 10))
        self.assertEqual(self.count("one", rule="rule", event=event), ViolationCount(1, 10))
        rows = self.rows()
        self.assertEqual([(row["rule_id"], json.loads(row["owner"])) for row in rows],
                         [("rule", ["rule"]), ("rule2", ["rule2"])])
        self.assertEqual(rows[0]["source_event_id"], rows[1]["source_event_id"])

    def test_shared_and_independent_units_on_one_message_stay_separate(self):
        event = self.judgment("one", rules=["rule", "rule2", "solo"])
        shared = ("rule", "rule2")
        self.assertEqual(self.count("one", rule="rule", event=event, shared=shared), ViolationCount(1, 10))
        self.assertEqual(self.count("one", rule="solo", event=event), ViolationCount(1, 10))
        self.assertEqual(self.count("one", rule="rule2", event=event, shared=shared), ViolationCount(1, 10))
        self.assertEqual(len(self.rows()), 2)

    def test_changing_the_group_changes_how_later_counts_are_calculated_not_old_rows(self):
        self.assertEqual(self.count("one", rule="rule"), ViolationCount(1, 10))
        self.assertEqual(self.count("two", rule="rule2"), ViolationCount(1, 10))
        shared = ("rule", "rule2")
        self.assertEqual(self.count("three", rule="rule", shared=shared), ViolationCount(3, 45))
        self.assertEqual([json.loads(row["owner"]) for row in self.rows()],
                         [["rule"], ["rule2"], ["rule", "rule2"]])

    def test_revoke_by_count_id_leaves_other_counts_of_the_message(self):
        event = self.judgment("one", rules=["rule", "rule2"])
        self.count("one", rule="rule", event=event)
        self.count("one", rule="rule2", event=event)
        first, second = [row["id"] for row in self.rows()]
        args = dict(platform_id="bot", group_id="group", message_id="one", revoked_at=START + EPSILON,
                    revoked_by="admin")
        self.assertFalse(self.write(revoke_violation, **args, count_id=second + 100))
        self.assertTrue(self.write(revoke_violation, **args, count_id=first))
        self.assertFalse(self.write(revoke_violation, **args, count_id=first))
        rows = {row["id"]: row for row in self.rows()}
        self.assertEqual(rows[first]["revoked_by"], "admin")
        self.assertIsNone(rows[second]["revoked_by"])
        self.assertIsNone(self.count("one", rule="rule", event=event, counted_at=START + EPSILON))
        self.assertEqual(self.count("one", rule="rule2", event=event, counted_at=START + EPSILON), ViolationCount(1, 10))
        for bad in (0, -1, True, "1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.write(revoke_violation, **args, count_id=bad)

    def test_revoke_without_count_id_revokes_every_unit_of_the_message_only(self):
        event = self.judgment("one", rules=["rule", "rule2"])
        self.count("one", rule="rule", event=event)
        self.count("one", rule="rule2", event=event)
        self.count("two")
        args = dict(platform_id="bot", group_id="group", message_id="one", revoked_at=START + EPSILON,
                    revoked_by="admin")
        self.assertTrue(self.write(revoke_violation, **args))
        revoked = {row["message_id"]: row["revoked_by"] for row in self.rows()}
        self.assertEqual(sorted(row["revoked_by"] or "" for row in self.rows()), ["", "admin", "admin"])
        self.assertIsNone(next(row["revoked_by"] for row in self.rows() if row["message_id"] == "two"))
        self.assertEqual(revoked["one"], "admin")

    def test_revoked_unit_is_excluded_from_the_ladder(self):
        shared = ("rule", "rule2")
        self.count("one", rule="rule", shared=shared)
        self.write(revoke_violation, platform_id="bot", group_id="group", message_id="one",
                   revoked_at=START + EPSILON, revoked_by="admin")
        self.assertEqual(self.count("two", rule="rule2", shared=shared, counted_at=START + EPSILON), ViolationCount(1, 10))

    def test_older_single_count_table_is_rebuilt_and_kept_for_rollback(self):
        path = Path(self.queue.path).with_name("old-counts.sqlite3")
        db = sqlite3.connect(path)
        try:
            with db:
                db.execute("""CREATE TABLE violation_counts (
                    platform_id TEXT NOT NULL, group_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL, rule_id TEXT NOT NULL,
                    source_event_id INTEGER NOT NULL UNIQUE,
                    admitted_at_us INTEGER NOT NULL, counted_at_us INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL CHECK(ordinal > 0),
                    duration_seconds INTEGER NOT NULL CHECK(duration_seconds BETWEEN 1 AND 2592000),
                    revoked_at_us INTEGER, revoked_by TEXT,
                    PRIMARY KEY(platform_id, group_id, message_id))""")
                db.execute("INSERT INTO violation_counts VALUES ('bot', 'group', 'm1', 'member', 'rule', 11, 1, 2, 1, 10, NULL, NULL)")
                db.execute("INSERT INTO violation_counts VALUES ('bot', 'group', 'm2', 'member', 'rule2', 12, 1, 3, 2, 30, 5, 'admin')")
        finally:
            db.close()
        for _ in range(2):
            DryRunQueue(path).initialize()
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute("SELECT * FROM violation_counts ORDER BY source_event_id").fetchall()
            self.assertEqual([(row["message_id"], row["rule_id"], json.loads(row["owner"]), row["ordinal"],
                               row["duration_seconds"], row["revoked_by"]) for row in rows],
                             [("m1", "rule", ["rule"], 1, 10, None), ("m2", "rule2", ["rule2"], 2, 30, "admin")])
            self.assertTrue(all(row["id"] for row in rows))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM violation_counts_legacy").fetchone()[0], 2)
            db.execute("INSERT INTO violation_counts (platform_id, group_id, message_id, owner, sender_id, rule_id, source_event_id,"
                       " admitted_at_us, counted_at_us, ordinal, duration_seconds) VALUES"
                       " ('bot', 'group', 'm1', '[\"rule2\"]', 'member', 'rule2', 11, 1, 2, 1, 10)")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO violation_counts (platform_id, group_id, message_id, owner, sender_id, rule_id, source_event_id,"
                           " admitted_at_us, counted_at_us, ordinal, duration_seconds) VALUES"
                           " ('bot', 'group', 'm1', '[\"rule2\"]', 'member', 'rule2', 13, 1, 2, 1, 10)")
        finally:
            db.close()

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
        self.assertEqual(self.count("one", counted_at=much_later), ViolationCount(1, 10))
        self.assertEqual(len(self.rows()), 2)
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
        self.assertNotIn(None, results)
        self.assertEqual(sorted(set((result.ordinal, result.duration_seconds) for result in results)),
                         [(1, 10), (2, 30), (3, 45), (4, 45)])
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.rows()), 4)
