import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sentinel.dry_run_queue import DryRunQueue
from sentinel.settings_store import SettingsStore


class PendingReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "state.sqlite"
        self.queue = DryRunQueue(self.path)
        self.queue.initialize()
        self.settings = SettingsStore(self.path, Path(temp.name) / "backup")
        self.config = self.settings.load_or_migrate({})
        self.config.update(monitoring_groups=["p/g"], action_execution_enabled=True,
                           decision_backend="llm", llm_provider_id="model")
        self.settings.save(self.config)
        self.clock = 0

    def tick(self):
        self.clock += 1
        return f"2026-01-01T00:00:{self.clock:02d}Z"

    def event(self, message, status="proposed", *, platform="p", group="g", detail=None):
        with closing(sqlite3.connect(self.path)) as db:
            event_id = db.execute("""INSERT INTO dry_run_events (platform_id, group_id, message_id, status, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""", (platform, group, message, status,
                json.dumps(detail if detail is not None else {"review_reason": "why"}), self.tick())).lastrowid
            db.commit()
        return event_id

    def intent(self, event_id, message, *, action="mute", status="pending_confirm", platform="p", group="g", index=0,
               mode="confirm", reason="rule"):
        stamp = self.tick()
        with closing(sqlite3.connect(self.path)) as db:
            intent_id = db.execute("""INSERT INTO execution_intents
                (source_event_id, source_attempt_id, platform_id, group_id, message_id, rule_id, rule_version,
                 action, target_sender_id, mode, duration_seconds, expires_at, status, created_at, updated_at,
                 plan_id, action_index, mode_reason)
                VALUES (?, 'attempt', ?, ?, ?, 'r', 1, ?, 'sender', ?, NULL, NULL, ?, ?, ?, ?, ?, ?)""",
                (event_id, platform, group, message, action, mode, status, stamp, stamp, f"event:{event_id}", index, reason)).lastrowid
            db.commit()
        return intent_id

    def reviews(self, **kwargs):
        if kwargs.pop("all_groups", True):
            return self.queue.pending_reviews(None, None, all_groups=True, **kwargs)
        return self.queue.pending_reviews(kwargs.pop("platform_id"), kwargs.pop("group_id"), **kwargs)

    def totals(self, **kwargs):
        result = self.reviews(**kwargs)
        return result["total_messages"], result["total_actions"], result["total_checks"]

    def test_every_action_type_is_listed_and_a_message_counts_once(self):
        event = self.event("a")
        for index, action in enumerate(("mute", "reply", "recall")):
            self.intent(event, "a", action=action, index=index)
        self.intent(event, "a", action="mute", status="unknown", index=3)
        self.assertEqual(self.totals(), (1, 3, 1))
        message = self.reviews()["messages"][0]
        self.assertEqual([item["action"] for item in message["actions"]], ["mute", "reply", "recall"])
        self.assertEqual(len(message["checks"]), 1)
        self.assertEqual(message["actions"][0]["mode_reason"], "rule")
        self.assertEqual(message["actions"][0]["status"], "pending_confirm")

    def test_only_waiting_manual_actions_and_unresolved_intents_are_open_items(self):
        event = self.event("a")
        for index, status in enumerate(("pending_auto", "pending_retry", "executing", "succeeded", "failed", "blocked",
                                        "permission_denied", "inactive", "dismissed")):
            self.intent(event, "a", status=status, index=index)
        self.assertEqual(self.totals(), (0, 0, 0))
        self.intent(event, "a", status="review", index=20)
        self.assertEqual(self.totals(), (1, 0, 1))

    def test_unknown_and_review_intents_are_checks_with_their_reason(self):
        event = self.event("a")
        first = self.intent(event, "a", status="unknown")
        second = self.intent(event, "b", status="review", index=1)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE execution_intents SET reason_code='intent_expired' WHERE id=?", (second,))
            db.commit()
        messages = {item["message_id"]: item for item in self.reviews()["messages"]}
        check = messages["a"]["checks"][0]
        self.assertEqual((check["kind"], check["intent_id"], check["status"], check["event_id"]), ("intent", first, "unknown", event))
        self.assertEqual(messages["b"]["checks"][0]["reason_code"], "intent_expired")
        self.assertEqual(messages["b"]["actions"], [])

    def test_latest_review_event_is_the_only_event_check(self):
        old = self.event("a", "review")
        self.assertEqual(self.totals(), (1, 0, 1))
        self.event("a", "proposed")
        self.assertEqual(self.totals(), (0, 0, 0))
        newest = self.event("a", "review", detail={"review_reason": "chat_context_unavailable"})
        result = self.reviews()
        self.assertEqual((result["total_messages"], result["total_checks"]), (1, 1))
        check = result["messages"][0]["checks"][0]
        self.assertEqual((check["kind"], check["event_id"], check["review_reason"]), ("event", newest, "chat_context_unavailable"))
        self.assertNotEqual(check["event_id"], old)

    def test_message_stays_until_every_item_is_closed(self):
        event = self.event("a", "review")
        first = self.intent(event, "a", action="mute")
        second = self.intent(event, "a", action="reply", index=1)
        self.assertEqual(self.totals(), (1, 2, 1))
        self.queue.close_review_item(target="action", actor="admin", intent_id=first)
        self.assertEqual(self.totals(), (1, 1, 1))
        self.assertEqual([item["id"] for item in self.reviews()["messages"][0]["actions"]], [second])
        self.queue.close_review_item(target="action", actor="admin", intent_id=second)
        self.assertEqual(self.totals(), (1, 0, 1))
        self.queue.close_review_item(target="event_check", actor="admin", event_id=event)
        self.assertEqual(self.totals(), (0, 0, 0))
        self.assertEqual(self.reviews()["messages"], [])

    def test_totals_count_each_message_once_but_every_item_separately(self):
        for name in "abc":
            event = self.event(name, "review")
            self.intent(event, name, action="mute")
            self.intent(event, name, action="recall", index=1)
        self.assertEqual(self.totals(), (3, 6, 3))

    def test_group_scope_and_system_platform_are_respected(self):
        self.intent(self.event("a"), "a")
        self.intent(self.event("b", group="other"), "b", group="other")
        self.intent(self.event("c", platform="q"), "c", platform="q")
        system = dict(platform="__rule_sentinel_system__", group="__ingress_overflow__")
        self.intent(self.event("d", **system), "d", platform=system["platform"], group=system["group"])
        self.assertEqual(self.totals(), (3, 3, 0))
        self.assertEqual(self.totals(all_groups=False, platform_id="p", group_id="g"), (1, 1, 0))
        self.assertEqual(self.totals(all_groups=False, platform_id="p", group_id="other"), (1, 1, 0))
        self.assertEqual(self.totals(all_groups=False, platform_id="p", group_id="none"), (0, 0, 0))
        for args, kwargs in ((("p", None), {"all_groups": True}), (("", "g"), {"all_groups": False}),
                             ((None, None), {"all_groups": 1}), ((None, None), {"all_groups": False})):
            with self.subTest(args=args, kwargs=kwargs), self.assertRaises(ValueError):
                self.queue.pending_reviews(*args, **kwargs)

    def test_pagination_walks_messages_newest_first_without_gaps_or_repeats(self):
        for index in range(7):
            event = self.event(f"m{index}")
            self.intent(event, f"m{index}")
            self.intent(event, f"m{index}", action="reply", index=1)
        seen, before = [], None
        while True:
            page = self.reviews(limit=3, before_id=before)
            seen += [item["message_id"] for item in page["messages"]]
            self.assertEqual((page["total_messages"], page["total_actions"]), (7, 14))
            before = page["next_before_id"]
            if before is None:
                break
        self.assertEqual(seen, [f"m{index}" for index in reversed(range(7))])
        last = self.reviews(limit=7)
        self.assertIsNone(last["next_before_id"])
        cursors = [item["cursor"] for item in last["messages"]]
        self.assertEqual(cursors, sorted(cursors, reverse=True))
        for bad in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"before_id": 0}, {"before_id": "1"}, {"before_id": True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.reviews(**bad)

    def test_oldest_open_item_time_is_reported(self):
        self.assertIsNone(self.reviews()["oldest_at"])
        first_event = self.event("a")
        self.intent(first_event, "a")
        result = self.reviews()
        with closing(sqlite3.connect(self.path)) as db:
            created = db.execute("SELECT created_at FROM execution_intents").fetchone()[0]
        self.assertEqual(result["oldest_at"], created)
        review = self.event("b", "review")
        with closing(sqlite3.connect(self.path)) as db:
            review_time = db.execute("SELECT created_at FROM dry_run_events WHERE id=?", (review,)).fetchone()[0]
        self.assertEqual(self.reviews()["oldest_at"], min(created, review_time))

    def test_execution_allowed_flag_follows_the_current_server_settings(self):
        self.intent(self.event("a"), "a")
        self.assertTrue(self.reviews()["messages"][0]["execution_allowed"])
        def store(payload):
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("UPDATE dashboard_settings SET payload=?", (payload,))
                db.commit()
        store(json.dumps({**self.config, "action_execution_enabled": False}))
        self.assertFalse(self.reviews()["messages"][0]["execution_allowed"])
        store(json.dumps({**self.config, "group_overrides": {"p/g": {"execution_mode": "record"}}}))
        self.assertFalse(self.reviews()["messages"][0]["execution_allowed"])
        store(json.dumps({**self.config, "group_overrides": {"other/g": {"execution_mode": "record"}}}))
        self.assertTrue(self.reviews()["messages"][0]["execution_allowed"])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TABLE dashboard_settings")
            db.commit()
        self.assertFalse(self.reviews()["messages"][0]["execution_allowed"])

    def test_dismissed_and_checked_items_leave_the_audit_intact(self):
        event = self.event("a", "review")
        action = self.intent(event, "a")
        check = self.intent(event, "a", status="unknown", index=1)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""INSERT INTO violation_counts (platform_id, group_id, message_id, owner, sender_id, rule_id,
                source_event_id, admitted_at_us, counted_at_us, ordinal, duration_seconds)
                VALUES ('p', 'g', 'a', '["r"]', 'sender', 'r', ?, 1, 2, 1, 60)""", (event,))
            db.commit()
            before = [tuple(row) for row in db.execute("SELECT * FROM violation_counts")]
        result = self.queue.close_review_item(target="action", actor="admin", intent_id=action)
        self.assertTrue(result["closed"] and not result["already_closed"])
        self.queue.close_review_item(target="intent_check", actor="reviewer", intent_id=check)
        self.queue.close_review_item(target="event_check", actor="reviewer", event_id=event)
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = {row["id"]: row for row in db.execute("SELECT * FROM execution_intents")}
            self.assertEqual((rows[action]["status"], rows[action]["reason_code"]), ("dismissed", "dismissed"))
            self.assertEqual(rows[check]["status"], "unknown")
            self.assertEqual(db.execute("SELECT count(*) FROM execution_attempts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM dry_run_events WHERE id=?", (event,)).fetchone()[0], "review")
            closures = [tuple(row) for row in db.execute("SELECT target, ref_id, kind, closed_by FROM review_closures ORDER BY id")]
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM violation_counts")], before)
        self.assertEqual(closures, [("intent", action, "dismissed", "admin"), ("intent", check, "checked", "reviewer"),
                                    ("event", event, "checked", "reviewer")])
        row = self.queue.list_messages(platform_id="p", group_id="g")[0]
        by_id = {item["id"]: item for item in row["executions"]}
        self.assertEqual(by_id[action]["closure"]["closed_by"], "admin")
        self.assertEqual(by_id[action]["closure"]["kind"], "dismissed")
        self.assertTrue(by_id[action]["closure"]["closed_at"])
        self.assertEqual(by_id[action]["mode_reason"], "rule")
        self.assertEqual(by_id[action]["rule_id"], "r")
        self.assertEqual(by_id[check]["closure"]["kind"], "checked")

    def test_closing_twice_is_idempotent_and_keeps_the_first_operator(self):
        action = self.intent(self.event("a"), "a")
        first = self.queue.close_review_item(target="action", actor="one", intent_id=action)
        second = self.queue.close_review_item(target="action", actor="two", intent_id=action)
        self.assertEqual((first["closed"], first["already_closed"]), (True, False))
        self.assertEqual((second["closed"], second["already_closed"]), (False, True))
        self.assertEqual((second["closed_by"], second["closed_at"]), ("one", first["closed_at"]))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM review_closures").fetchone()[0], 1)

    def test_wrong_state_unknown_or_malformed_items_are_refused(self):
        event = self.event("a")
        waiting = self.intent(event, "a")
        running = self.intent(event, "a", status="pending_auto", mode="auto", reason=None, index=1)
        unknown = self.intent(event, "a", status="unknown", index=2)
        succeeded = self.intent(event, "a", status="succeeded", index=3)
        refused = (("action", running), ("action", unknown), ("action", succeeded), ("action", 9999),
                   ("intent_check", waiting), ("intent_check", running), ("intent_check", succeeded),
                   ("intent_check", 9999))
        for target, intent_id in refused:
            with self.subTest(target=target, intent_id=intent_id), self.assertRaises(ValueError):
                self.queue.close_review_item(target=target, actor="admin", intent_id=intent_id)
        for kwargs in ({"target": "other", "intent_id": waiting}, {"target": "action"},
                       {"target": "action", "intent_id": True}, {"target": "action", "intent_id": 0},
                       {"target": "action", "intent_id": waiting, "event_id": event},
                       {"target": "event_check", "intent_id": waiting}, {"target": "event_check", "event_id": 9999}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.queue.close_review_item(actor="admin", **kwargs)
        with self.assertRaises(ValueError):
            self.queue.close_review_item(target="action", actor=" ", intent_id=waiting)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM review_closures").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM execution_intents WHERE id=?", (waiting,)).fetchone()[0], "pending_confirm")

    def test_only_the_latest_review_event_can_be_marked_checked(self):
        old = self.event("a", "review")
        self.event("a", "no_action")
        with self.assertRaises(ValueError):
            self.queue.close_review_item(target="event_check", actor="admin", event_id=old)
        newest = self.event("a", "review")
        with self.assertRaises(ValueError):
            self.queue.close_review_item(target="event_check", actor="admin", event_id=old)
        self.assertTrue(self.queue.close_review_item(target="event_check", actor="admin", event_id=newest)["closed"])
        self.assertTrue(self.queue.close_review_item(target="event_check", actor="x", event_id=newest)["already_closed"])
        proposed = self.event("b", "proposed")
        with self.assertRaises(ValueError):
            self.queue.close_review_item(target="event_check", actor="admin", event_id=proposed)

    def test_a_new_review_event_reopens_the_message_after_an_older_one_was_checked(self):
        old = self.event("a", "review")
        self.queue.close_review_item(target="event_check", actor="admin", event_id=old)
        self.assertEqual(self.totals(), (0, 0, 0))
        self.event("a", "review")
        self.assertEqual(self.totals(), (1, 0, 1))

    def test_closing_publishes_one_change_for_the_message_only_when_it_closes(self):
        action = self.intent(self.event("a"), "a")
        published = []
        self.queue._publish = lambda key, event_id: published.append((key, event_id))
        self.queue.close_review_item(target="action", actor="admin", intent_id=action)
        self.queue.close_review_item(target="action", actor="admin", intent_id=action)
        self.assertEqual(published, [(("p", "g", "a"), 1)])

    def test_execution_summary_reports_dismissed_outcomes(self):
        event = self.event("a")
        first = self.intent(event, "a", action="mute")
        second = self.intent(event, "a", action="reply", index=1)
        self.queue.close_review_item(target="action", actor="admin", intent_id=first)
        self.assertEqual(self.queue.list_messages(platform_id="p", group_id="g")[0]["execution_summary"], "pending_confirm")
        self.queue.close_review_item(target="action", actor="admin", intent_id=second)
        self.assertEqual(self.queue.list_messages(platform_id="p", group_id="g")[0]["execution_summary"], "dismissed")
        self.assertEqual(DryRunQueue._execution_summary([{"status": "succeeded"}, {"status": "dismissed"}]), "partial_dismissed")
        self.assertEqual(DryRunQueue._execution_summary([{"status": "pending_confirm"}, {"status": "dismissed"}]), "pending_confirm")
        self.assertEqual(DryRunQueue._execution_summary([{"status": "failed"}, {"status": "dismissed"}]), "partial_failed")


if __name__ == "__main__":
    unittest.main()
