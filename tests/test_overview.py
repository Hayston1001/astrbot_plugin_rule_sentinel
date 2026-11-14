import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sentinel.dry_run_queue import DryRunQueue
from sentinel.management_service import ManagementService
from sentinel.rule_store import RuleStore


class OverviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "state.sqlite"
        self.rules = RuleStore(self.path)
        self.rules.initialize()
        self.queue = DryRunQueue(self.path)
        self.queue.initialize()
        self.service = ManagementService(self.rules, self.queue)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE dashboard_settings (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("INSERT INTO dashboard_settings VALUES (1, ?)", (json.dumps({"message_retention_days": 30}),))
            db.commit()

    def add_action(self, platform, group, message, *, status="pending_confirm", mode="confirm", action="mute", index=0):
        with closing(sqlite3.connect(self.path)) as db:
            event = db.execute("""INSERT INTO dry_run_events
                (platform_id, group_id, message_id, status, detail, created_at)
                VALUES (?, ?, ?, 'proposed', ?, '2025-01-01T00:00:00Z')""",
                (platform, group, message, json.dumps({"proposal": {"reason": "reason"}}))).lastrowid
            db.execute("""INSERT INTO execution_intents
                (source_event_id, source_attempt_id, platform_id, group_id, message_id, rule_id, rule_version,
                 action, target_sender_id, mode, duration_seconds, expires_at, status, created_at, updated_at,
                 plan_id, action_index) VALUES (?, 'attempt', ?, ?, ?, 'r', 1, ?, 'sender', ?, 60,
                 '2025-01-01T00:01:00+00:00', ?, '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z', ?, ?)""",
                (event, platform, group, message, action, mode, status, f"event:{event}", index))
            db.commit()

    def test_layout_is_user_scoped_validated_and_preserves_future_cards(self):
        layout = {"cards": [{"id": "pending", "width": "full", "visible": False},
                            {"id": "future-card", "width": "half", "visible": True}]}
        self.assertEqual(self.service.get_overview_layout("admin"), {"cards": []})
        self.assertEqual(self.service.save_overview_layout("admin", layout), layout)
        self.assertEqual(self.service.get_overview_layout("admin"), layout)
        self.assertEqual(self.service.get_overview_layout("other"), {"cards": []})
        self.assertEqual(self.service.save_ui_state("admin", {"active_tab": "records"}), {"active_tab": "records"})
        self.assertEqual(self.service.get_overview_layout("admin"), layout)
        self.assertEqual(ManagementService(self.rules, DryRunQueue(self.path)).get_overview_layout("admin"), layout)
        for invalid in ({}, {"cards": None}, {"cards": [{"id": "pending", "width": "full", "visible": 1}]},
                        {"cards": [{"id": "Pending", "width": "full", "visible": True}]},
                        {"cards": [layout["cards"][0], layout["cards"][0]]},
                        {"cards": [{"id": "pending", "width": "wide", "visible": True}]}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.service.save_overview_layout("admin", invalid)
        self.assertEqual(self.service.get_overview_layout("admin"), layout)
        for actor in (None, "", "api_key:key"):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                self.service.save_overview_layout(actor, layout)
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                self.service.get_overview_layout(actor)

    def test_pending_is_scoped_grouped_and_paged_without_time_cutoff(self):
        for index in range(25):
            self.add_action("p", "g", f"m{index}")
        self.add_action("p", "g", "m0")
        self.add_action("p2", "g", "m0")
        for status, mode, action in (("review", "confirm", "mute"), ("unknown", "confirm", "mute"),
                                     ("pending_auto", "auto", "mute"), ("pending_confirm", "confirm", "reply")):
            self.add_action("p", "g", f"excluded-{status}-{action}", status=status, mode=mode, action=action)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("INSERT INTO message_contents (platform_id, group_id, message_id, content) VALUES ('p', 'g', 'm0', ?)", ("x" * 500,))
            db.commit()
        first = self.service.pending_confirmations("admin", None, None, limit=10, all_groups=True)
        self.assertEqual((first["total_messages"], first["total_actions"]), (26, 27))
        self.assertEqual(len(first["messages"]), 10)
        second = self.service.pending_confirmations("admin", None, None, before_id=first["next_before_id"], limit=100, all_groups=True)
        items = first["messages"] + second["messages"]
        self.assertEqual(len(items), 26)
        self.assertEqual(len({(item["platform_id"], item["group_id"], item["message_id"]) for item in items}), 26)
        target = next(item for item in items if item["platform_id"] == "p" and item["message_id"] == "m0")
        self.assertEqual(len(target["actions"]), 2)
        self.assertEqual(len(target["content"]), 400)
        self.assertTrue(target["content_truncated"])
        self.assertTrue(all(action["created_at"].startswith("2025-") for action in target["actions"]))
        scoped = self.service.pending_confirmations("admin", "p2", "g")
        self.assertEqual((scoped["total_messages"], scoped["total_actions"]), (1, 1))
        self.assertIsNone(scoped["messages"][0]["content"])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE dashboard_settings SET payload=?", (json.dumps({"message_retention_days": 0}),))
            db.commit()
        self.assertIsNone(self.service.pending_confirmations("admin", "p", "g")["messages"][0]["content"])
        for actor in (None, "", "api_key:key"):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                self.service.pending_confirmations(actor, None, None, all_groups=True)
        with self.assertRaises(ValueError):
            self.service.pending_confirmations("admin", "p", "g", limit=0)


if __name__ == "__main__":
    unittest.main()
