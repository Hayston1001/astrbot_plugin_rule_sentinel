import sqlite3
import json
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from sentinel.dry_run_queue import DryRunQueue
from sentinel.rule_store import RuleStore


class QueueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "queue.sqlite"
        self.queue = DryRunQueue(self.path)
        self.queue.initialize()

    def enqueue(self, message, group="g", capacity=2):
        return self.queue.enqueue(platform_id="p", group_id=group, message_id=message,
                                  payload={"text": "private"}, capacity=capacity)

    def test_capacity_dedup_order_and_group_isolation(self):
        self.assertEqual(self.enqueue("1"), "queued")
        self.assertEqual(self.enqueue("1"), "duplicate")
        self.assertEqual(self.enqueue("2"), "queued")
        self.assertEqual(self.enqueue("3"), "queue_full")
        self.assertEqual(self.enqueue("3", group="other"), "queued")
        first = self.queue.claim("p", "g")
        self.assertEqual(first.message_id, "1")
        self.assertIsNone(self.queue.claim("p", "g"))
        self.assertTrue(self.queue.finish(first, status="no_action", detail="no_tool_calls"))
        self.assertEqual(self.queue.claim("p", "g").message_id, "2")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT payload FROM dry_run_queue WHERE message_id='1'").fetchone()[0], None)
            self.assertEqual(db.execute("SELECT status, detail FROM dry_run_events ORDER BY id").fetchall(), [("skipped", "queue_full"), ("no_action", "no_tool_calls")])

    def test_recovery_invalidates_old_lease(self):
        self.enqueue("1")
        old = self.queue.claim("p", "g")
        self.assertEqual(self.queue.recover_after_exclusive_start(), 1)
        current = DryRunQueue(self.path).claim("p", "g")
        self.assertFalse(self.queue.finish(old, status="no_action", detail="stale"))
        self.assertTrue(self.queue.finish(current, status="failed", detail="model_error"))
        self.assertEqual(self.enqueue("1"), "duplicate")

    def test_corrupt_payload_is_audited_cleared_and_does_not_block_group(self):
        self.enqueue("1", capacity=4)
        self.enqueue("2", capacity=4)
        self.enqueue("3", capacity=4)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE dry_run_queue SET payload=? WHERE message_id='1'", ('{"text":"private',))
            db.execute("UPDATE dry_run_queue SET payload=? WHERE message_id='2'", ('["private"]',))
            db.commit()
        item = self.queue.claim("p", "g")
        self.assertEqual(item.message_id, "3")
        self.assertEqual(item.payload, {"text": "private"})
        events = self.queue.list_events(platform_id="p", group_id="g")
        self.assertEqual([(event["message_id"], event["status"], event["detail"]) for event in events],
                         [("1", "failed", "invalid_queue_payload"), ("2", "failed", "invalid_queue_payload")])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT state, payload FROM dry_run_queue WHERE message_id IN ('1', '2') ORDER BY id").fetchall(),
                             [("done", None), ("done", None)])
        self.assertEqual(self.enqueue("1", capacity=4), "duplicate")

    def test_corrupt_payload_quarantine_rolls_back_if_audit_fails(self):
        self.enqueue("1")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE dry_run_queue SET payload='not json'")
            db.execute("CREATE TRIGGER fail_event BEFORE INSERT ON dry_run_events BEGIN SELECT RAISE(ABORT, 'failure'); END")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.queue.claim("p", "g")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT state, payload, lease FROM dry_run_queue").fetchone(),
                             ("pending", "not json", None))

    def test_excessively_nested_payload_does_not_strand_group(self):
        self.enqueue("1")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE dry_run_queue SET payload=?", ("[" * 2000 + "]" * 2000,))
            db.commit()
        self.assertIsNone(self.queue.claim("p", "g"))
        events = self.queue.list_events(platform_id="p", group_id="g")
        self.assertEqual([(e["status"], e["detail"]) for e in events], [("failed", "invalid_queue_payload")])

    def test_failed_audit_rolls_back_completion(self):
        self.enqueue("1")
        item = self.queue.claim("p", "g")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER fail_event BEFORE INSERT ON dry_run_events BEGIN SELECT RAISE(ABORT, 'failure'); END")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.queue.finish(item, status="no_action", detail="no_tool_calls")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT state, payload FROM dry_run_queue").fetchone()[0], "processing")

    def test_rule_version_change_at_commit_requires_review(self):
        rules = RuleStore(self.path)
        rules.initialize()
        fields = dict(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                      text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                      severity_seconds={"low": 60}, modified_by="admin")
        rules.save(**fields)
        self.enqueue("1")
        item = self.queue.claim("p", "g")
        rules.save(**{**fields, "expected_version": 1, "text": "changed"})
        self.assertTrue(self.queue.finish(item, status="proposed", detail='{"snapshot_current":true}',
                                          rule_versions=(("r", 1),)))
        event = self.queue.list_events(platform_id="p", group_id="g")[0]
        self.assertEqual(event["status"], "review")
        detail = json.loads(event["detail"])
        self.assertEqual(detail["review_reason"], "rule_snapshot_changed_at_commit")
        self.assertEqual(detail["original_status"], "proposed")
        self.assertTrue(detail["decision"]["snapshot_current"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT state, payload FROM dry_run_queue").fetchone(), ("done", None))

    def test_rule_commit_guard_detects_new_and_disabled_rules(self):
        rules = RuleStore(self.path)
        rules.initialize()
        fields = dict(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                      text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                      severity_seconds={"low": 60}, modified_by="admin")
        rules.save(**fields)
        self.enqueue("1")
        item = self.queue.claim("p", "g")
        rules.save(**{**fields, "rule_id": "r2"})
        self.assertTrue(self.queue.finish(item, status="no_action", detail="{}", rule_versions=(("r", 1),)))
        self.assertEqual(self.queue.list_events(platform_id="p", group_id="g")[0]["status"], "review")
        self.enqueue("2")
        item = self.queue.claim("p", "g")
        rules.save(**{**fields, "expected_version": 1, "enabled": False})
        self.assertTrue(self.queue.finish(item, status="no_action", detail="{}",
                                         rule_versions=(("r", 1), ("r2", 1))))
        self.assertEqual(self.queue.list_events(platform_id="p", group_id="g")[1]["status"], "review")

    def test_rule_commit_guard_waits_for_concurrent_rule_transaction(self):
        rules = RuleStore(self.path)
        rules.initialize()
        fields = dict(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                      text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                      severity_seconds={"low": 60}, modified_by="admin")
        rules.save(**fields)
        self.enqueue("1")
        item = self.queue.claim("p", "g")
        entered = threading.Event()
        original_connect = self.queue._connect
        def observed_connect():
            db = original_connect()
            entered.set()
            return db
        self.queue._connect = observed_connect
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO rule_versions
                (platform_id, group_id, rule_id, version, text, enabled, allowed_actions,
                 duration_mode, severity_seconds, modified_by, priority)
                VALUES ('p', 'g', 'r', 2, 'changed', 1, '["mute"]', 'strict', '{"low":60}', 'admin', 0)""")
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.queue.finish, item, status="no_action", detail="{}",
                                     rule_versions=(("r", 1),))
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertFalse(future.done())
                finally:
                    db.commit()
                self.assertTrue(future.result(timeout=3))
        event = self.queue.list_events(platform_id="p", group_id="g")[0]
        self.assertEqual(event["status"], "review")
        self.assertEqual(json.loads(event["detail"])["review_reason"], "rule_snapshot_changed_at_commit")

    def test_rule_commit_guard_rolls_back_if_review_audit_fails(self):
        rules = RuleStore(self.path)
        rules.initialize()
        fields = dict(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                      text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                      severity_seconds={"low": 60}, modified_by="admin")
        rules.save(**fields)
        self.enqueue("1")
        item = self.queue.claim("p", "g")
        rules.save(**{**fields, "expected_version": 1, "enabled": False})
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER fail_review BEFORE INSERT ON dry_run_events WHEN NEW.status='review' BEGIN SELECT RAISE(ABORT, 'failure'); END")
            db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.queue.finish(item, status="proposed", detail="{}", rule_versions=(("r", 1),))
        with closing(sqlite3.connect(self.path)) as db:
            state, payload, lease = db.execute("SELECT state, payload, lease FROM dry_run_queue").fetchone()
            self.assertEqual((state, lease), ("processing", item.lease))
            self.assertEqual(json.loads(payload)["text"], "private")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dry_run_events").fetchone()[0], 0)

    def test_rule_commit_guard_only_compares_the_triggering_group(self):
        rules = RuleStore(self.path)
        rules.initialize()
        fields = dict(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                      text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                      severity_seconds={"low": 60}, modified_by="admin")
        rules.save(**fields)
        self.enqueue("1")
        item = self.queue.claim("p", "g")
        rules.save(**{**fields, "group_id": "other"})
        rules.save(**{**fields, "platform_id": "other"})
        self.assertTrue(self.queue.finish(item, status="no_action", detail="{}", rule_versions=(("r", 1),)))
        self.assertEqual(self.queue.list_events(platform_id="p", group_id="g")[0]["status"], "no_action")

    def test_unavailable_rules_at_commit_preserve_lease_and_payload(self):
        self.enqueue("1")
        item = self.queue.claim("p", "g")
        # The queue-only database has no rule_versions table.
        with self.assertRaises(sqlite3.OperationalError):
            self.queue.finish(item, status="no_action", detail="{}", rule_versions=(("r", 1),))
        with closing(sqlite3.connect(self.path)) as db:
            state, payload, lease = db.execute("SELECT state, payload, lease FROM dry_run_queue").fetchone()
            self.assertEqual((state, lease), ("processing", item.lease))
            self.assertEqual(json.loads(payload)["text"], "private")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dry_run_events").fetchone()[0], 0)

    def test_bad_inputs(self):
        with self.assertRaises(ValueError):
            self.enqueue("1", capacity=True)
        with self.assertRaises(ValueError):
            self.enqueue("")

    def test_audit_pagination_and_group_isolation(self):
        self.queue.record_skip(platform_id="p", group_id="g", message_id="1", reason="queue_full")
        self.queue.record_skip(platform_id="p", group_id="other", message_id="2", reason="queue_full")
        self.queue.record_skip(platform_id="p", group_id="g", message_id="3", reason="non_text")
        first = self.queue.list_events(platform_id="p", group_id="g", limit=1)
        second = self.queue.list_events(platform_id="p", group_id="g", after_id=first[0]["id"], limit=1)
        self.assertEqual([first[0]["message_id"], second[0]["message_id"]], ["1", "3"])
        for kwargs in ({"limit": 101}, {"after_id": True}, {"limit": 0}):
            with self.assertRaises(ValueError):
                self.queue.list_events(platform_id="p", group_id="g", **kwargs)

    def test_existing_audit_schema_is_migrated_without_losing_data(self):
        old_path = self.path.parent / "old.sqlite"
        with closing(sqlite3.connect(old_path)) as db:
            db.execute("CREATE TABLE dry_run_events (id INTEGER PRIMARY KEY, platform_id TEXT NOT NULL, group_id TEXT NOT NULL, message_id TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL)")
            db.execute("INSERT INTO dry_run_events VALUES (1, 'p', 'g', 'm', 'skipped', 'old', 'time')")
            db.commit()
        old = DryRunQueue(old_path)
        old.initialize()
        old.initialize()
        rows = old.list_events(platform_id="p", group_id="g")
        self.assertEqual(rows[0]["detail"], "old")
        self.assertIsNone(rows[0]["attempt_id"])

    def test_concurrent_enqueues_respect_capacity(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda index: self.enqueue(str(index), capacity=2), range(8)))
        self.assertEqual(results.count("queued"), 2)
        self.assertEqual(results.count("queue_full"), 6)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dry_run_queue").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
