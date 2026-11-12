import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sentinel.dry_run_queue import DryRunQueue
from sentinel.management_service import ManagementService
from sentinel.rule_store import RuleStore
from sentinel.settings_store import SettingsStore, validate_settings


class MessageRetentionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "state.sqlite"
        self.queue, self.rules = DryRunQueue(self.path), RuleStore(self.path)
        self.queue.initialize()
        self.rules.initialize()
        self.settings = SettingsStore(self.path, Path(temp.name) / "backup")
        self.config = self.settings.load_or_migrate({})
        self.service = ManagementService(self.rules, self.queue)

    def enqueue(self, message_id, group="g", text="original <b>text</b>"):
        return self.queue.enqueue(platform_id="p", group_id=group, message_id=message_id, capacity=10,
                                  payload={"text": "processed", "original_text": text, "sender_id": "member"})

    def content(self, message_id="m", group="g"):
        return self.queue.message_content(platform_id="p", group_id=group, message_id=message_id)

    def test_content_is_scoped_and_kept_after_queue_is_cleared(self):
        self.assertEqual(self.config["message_retention_days"], 30)
        self.enqueue("m")
        self.enqueue("m", group="other", text="other group")
        self.assertEqual(self.content(), "original <b>text</b>")
        self.assertEqual(self.content(group="other"), "other group")
        item = self.queue.claim("p", "g")
        self.queue.finish(item, status="no_action", detail="{}")
        self.assertEqual(self.content(), "original <b>text</b>")
        self.assertNotIn("original <b>text</b>", json.dumps(self.queue.list_messages(platform_id="p", group_id="g")))
        self.assertNotIn("original <b>text</b>", json.dumps(self.queue.list_events(platform_id="p", group_id="g")))
        self.assertEqual(self.service.message_content("admin", "p", "g", "m"), {"content": "original <b>text</b>"})
        for actor in ("", "api_key:token"):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                self.service.message_content(actor, "p", "g", "m")

    def test_expiry_and_shorter_retention_remove_old_content(self):
        self.enqueue("old")
        self.enqueue("recent")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE message_contents SET stored_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-31 days') WHERE message_id='old'")
            db.execute("UPDATE message_contents SET stored_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-3 days') WHERE message_id='recent'")
            db.commit()
        self.assertIsNone(self.content("old"))
        self.assertIsNotNone(self.content("recent"))
        self.assertEqual(self.queue.purge_message_contents(), 1)
        self.config["message_retention_days"] = 2
        self.settings.save(self.config)
        self.assertIsNone(self.content("recent"))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM message_contents").fetchone()[0], 0)

    def test_zero_disables_retention_and_invalid_settings_fail_closed(self):
        self.enqueue("before")
        self.config["message_retention_days"] = 0
        self.settings.save(self.config)
        self.enqueue("after")
        with closing(sqlite3.connect(self.path)) as db:
            payload = json.loads(db.execute("SELECT payload FROM dry_run_queue WHERE message_id='after'").fetchone()[0])
        self.assertEqual(payload["text"], "processed")
        self.assertNotIn("original_text", payload)
        self.assertIsNone(self.content("before"))
        self.assertIsNone(self.content("after"))
        for value in (-1, 36501, True, "30"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_settings({**self.config, "message_retention_days": value})
        broken = dict(self.config)
        broken["message_retention_days"] = "bad"
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE dashboard_settings SET payload=?", (json.dumps(broken),))
            db.commit()
        with self.assertRaises(ValueError):
            self.content("after")
        with self.assertRaises(ValueError):
            self.enqueue("invalid")

    def test_unqueued_messages_are_not_retained(self):
        self.queue.enqueue(platform_id="p", group_id="g", message_id="first", capacity=1, payload={"text": "first"})
        self.assertEqual(self.queue.enqueue(platform_id="p", group_id="g", message_id="full", capacity=1,
                                            payload={"text": "not stored"}), "queue_full")
        self.assertIsNone(self.content("full"))

    def test_disabling_retention_serializes_with_concurrent_enqueue(self):
        gate = threading.Barrier(2)
        disabled = {**self.config, "message_retention_days": 0}
        def enqueue():
            gate.wait()
            self.enqueue("concurrent")
        def save():
            gate.wait()
            self.settings.save(disabled)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(enqueue), pool.submit(save)]
            for future in futures:
                future.result(timeout=10)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM message_contents").fetchone()[0], 0)
