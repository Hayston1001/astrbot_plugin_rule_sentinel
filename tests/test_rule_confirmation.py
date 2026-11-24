import asyncio
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from sentinel.dry_run_host import DryRunHost
from sentinel.dry_run_queue import DryRunQueue
from sentinel.management_service import ManagementService


class RuleConfirmationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "state.sqlite"
        self.queue = DryRunQueue(self.path)
        self.queue.initialize()
        self.service = ManagementService(None, self.queue)
        self.clock = 0
        self.plans = 0

    def event(self, message="m", *, platform="p", group="g"):
        self.clock += 1
        with closing(sqlite3.connect(self.path)) as db:
            event_id = db.execute("""INSERT INTO dry_run_events
                (platform_id, group_id, message_id, status, detail, created_at)
                VALUES (?, ?, ?, 'proposed', '{}', ?)""",
                (platform, group, message, f"2026-01-01T00:00:{self.clock:02d}Z")).lastrowid
            db.commit()
        return event_id

    def intent(self, event_id, *, rule="a", action="mute", index=None, status="pending_confirm", message="m",
               platform="p", group="g"):
        self.plans += 1
        index = 1000 + self.plans if index is None else index
        with closing(sqlite3.connect(self.path)) as db:
            intent_id = db.execute("""INSERT INTO execution_intents
                (source_event_id, source_attempt_id, platform_id, group_id, message_id, rule_id, rule_version,
                 action, target_sender_id, mode, duration_seconds, expires_at, status, created_at, updated_at,
                 plan_id, action_index, mode_reason)
                VALUES (?, 'attempt', ?, ?, ?, ?, 1, ?, 'sender', 'confirm', 60, NULL, ?,
                 '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', ?, ?, 'rule')""",
                (event_id, platform, group, message, rule, action, status, f"plan-{self.plans}", index)).lastrowid
            db.commit()
        return intent_id

    def statuses(self):
        with closing(sqlite3.connect(self.path)) as db:
            return dict(db.execute("SELECT id, status FROM execution_intents").fetchall())

    def closures(self):
        with closing(sqlite3.connect(self.path)) as db:
            return [tuple(row) for row in db.execute(
                "SELECT target, ref_id, kind, closed_by, closed_at FROM review_closures ORDER BY id")]

    def rule_scope(self, **changes):
        return {"platform_id": "p", "group_id": "g", "message_id": "m", "rule_id": "a", **changes}

    def publish_spy(self):
        published = []
        self.queue._publish = lambda key, event_id: published.append((key, event_id))
        return published

    # DryRunQueue.pending_rule_actions

    def test_pending_rule_actions_lists_only_that_rules_waiting_actions_in_order(self):
        first = self.event()
        second = self.event("other")
        reply = self.intent(first, action="reply", index=1)
        mute = self.intent(first, action="mute", index=0)
        recall_auto = self.intent(first, action="recall", status="pending_auto")
        recall_review = self.intent(first, action="recall", status="review")
        other_rule = self.intent(first, rule="b")
        other_message = self.intent(second, message="other")
        self.assertEqual(self.queue.pending_rule_actions(**self.rule_scope()), [mute, reply])
        self.assertEqual(self.queue.pending_rule_actions(**self.rule_scope(rule_id="b")), [other_rule])
        self.assertEqual(self.queue.pending_rule_actions(**self.rule_scope(message_id="other")), [other_message])
        self.assertEqual(self.queue.pending_rule_actions(**self.rule_scope(rule_id="missing")), [])
        self.assertNotIn(recall_auto, self.queue.pending_rule_actions(**self.rule_scope()))
        self.assertNotIn(recall_review, self.queue.pending_rule_actions(**self.rule_scope()))

    def test_pending_rule_actions_rejects_invalid_identities(self):
        for key in ("platform_id", "group_id", "message_id", "rule_id"):
            for value in ("", " ", None, 1, True, "x" * 513):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.queue.pending_rule_actions(**self.rule_scope(**{key: value}))
        self.assertEqual(self.queue.pending_rule_actions(**self.rule_scope(message_id="x" * 512)), [])

    # DryRunQueue.close_rule_actions

    def test_close_rule_actions_dismisses_one_rules_actions_and_keeps_the_rest(self):
        event = self.event()
        mute = self.intent(event, action="mute")
        reply = self.intent(event, action="reply")
        auto = self.intent(event, action="recall", index=2, status="pending_auto")
        other_rule = self.intent(event, rule="b", action="mute")
        other_message = self.intent(event, message="other", rule="a")
        published = self.publish_spy()
        result = self.queue.close_rule_actions(**self.rule_scope(), actor="admin")
        self.assertEqual((result["closed"], result["already_closed"], result["closed_by"]), (2, 0, "admin"))
        self.assertTrue(result["closed_at"])
        statuses = self.statuses()
        self.assertEqual((statuses[mute], statuses[reply]), ("dismissed", "dismissed"))
        self.assertEqual(statuses[auto], "pending_auto")
        self.assertEqual(statuses[other_rule], "pending_confirm")
        self.assertEqual(statuses[other_message], "pending_confirm")
        closures = self.closures()
        self.assertEqual([(row[0], row[1], row[2], row[3]) for row in closures],
                         [("intent", mute, "dismissed", "admin"), ("intent", reply, "dismissed", "admin")])
        self.assertEqual(len({row[4] for row in closures}), 1)
        self.assertEqual(published, [(("p", "g", "m"), event)])
        with closing(sqlite3.connect(self.path)) as db:
            reasons = dict(db.execute("SELECT id, reason_code FROM execution_intents").fetchall())
        self.assertEqual((reasons[mute], reasons[reply]), ("dismissed", "dismissed"))
        self.assertIsNone(reasons[auto])
        self.assertIsNone(reasons[other_rule])
        review = self.queue.pending_reviews("p", "g")
        self.assertEqual([(item["message_id"], [action["id"] for action in item["actions"]])
                          for item in review["messages"]], [("m", [other_rule]), ("other", [other_message])])
        self.assertEqual(review["total_actions"], 2)

    def test_repeating_a_rule_close_reports_already_closed(self):
        """The documented contract: a second call closes nothing and reports the first operator."""
        event = self.event()
        first = self.intent(event, action="mute")
        second = self.intent(event, action="reply")
        self.queue.close_rule_actions(**self.rule_scope(), actor="admin")
        published = self.publish_spy()
        again = self.queue.close_rule_actions(**self.rule_scope(), actor="other")
        self.assertEqual((again["closed"], again["already_closed"]), (0, 2))
        self.assertEqual([row[3] for row in self.closures()], ["admin", "admin"])
        self.assertEqual(published, [])
        self.assertEqual(self.statuses(), {first: "dismissed", second: "dismissed"})
        self.assertEqual(self.queue.pending_rule_actions(**self.rule_scope()), [])

    def test_a_repeated_rule_close_never_publishes_or_rewrites_the_audit(self):
        event = self.event()
        self.intent(event, action="mute")
        self.queue.close_rule_actions(**self.rule_scope(), actor="admin")
        published = self.publish_spy()
        again = self.queue.close_rule_actions(**self.rule_scope(), actor="other")
        self.assertEqual((again["closed"], again["already_closed"], again["closed_by"]), (0, 1, "admin"))
        self.assertEqual(published, [])
        self.assertEqual([row[3] for row in self.closures()], ["admin"])

    def test_close_rule_actions_refuses_a_scope_without_waiting_actions(self):
        event = self.event()
        self.intent(event, action="recall", status="pending_auto")
        self.intent(event, rule="b")
        for scope in (self.rule_scope(rule_id="missing"), self.rule_scope(message_id="other"),
                      self.rule_scope(group_id="other"), self.rule_scope(platform_id="other")):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                self.queue.close_rule_actions(**scope, actor="admin")
        for actor in ("", " ", None, 1):
            with self.subTest(actor=actor), self.assertRaises(ValueError):
                self.queue.close_rule_actions(**self.rule_scope(rule_id="b"), actor=actor)
        for key in ("platform_id", "group_id", "message_id", "rule_id"):
            for value in ("", None, "x" * 513):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.queue.close_rule_actions(**self.rule_scope(**{key: value}), actor="admin")
        self.assertEqual(self.closures(), [])

    # DryRunHost.confirm_rule_actions

    class FakeExecutor:
        def __init__(self, test, outcomes, *, raises=False):
            self.test, self.outcomes, self.raises = test, list(outcomes), raises
            self.calls = []

        async def execute(self, intent_id, *, mode, **options):
            self.calls.append((intent_id, mode, options))
            if self.raises:
                raise RuntimeError("executor crashed")
            status = self.outcomes.pop(0) if self.outcomes else "succeeded"
            with closing(sqlite3.connect(self.test.path)) as db:
                db.execute("UPDATE execution_intents SET status=? WHERE id=?", (status, intent_id))
                db.commit()
            return status

    def host(self, executor):
        host = DryRunHost(context=None, config={}, rules=None, queue=self.queue, lock_path="unused")
        host.mute_executor = executor
        return host

    def test_rule_confirmation_runs_every_waiting_action_in_order(self):
        event = self.event()
        mute = self.intent(event, action="mute")
        reply = self.intent(event, action="reply")
        recall = self.intent(event, action="recall")
        auto = self.intent(event, rule="a", action="recall", status="pending_auto")
        other_rule = self.intent(event, rule="b", action="mute")
        executor = self.FakeExecutor(self, ["succeeded"] * 3)
        result = asyncio.run(self.host(executor).confirm_rule_actions(**self.rule_scope()))
        self.assertEqual(result, {"actions": [{"intent_id": mute, "status": "succeeded"},
                                              {"intent_id": reply, "status": "succeeded"},
                                              {"intent_id": recall, "status": "succeeded"}],
                                  "stopped": False, "remaining": 0})
        self.assertEqual(executor.calls, [(mute, "confirm", {}), (reply, "confirm", {}), (recall, "confirm", {})])
        statuses = self.statuses()
        self.assertEqual(statuses[auto], "pending_auto")
        self.assertEqual(statuses[other_rule], "pending_confirm")

    def test_rule_confirmation_continues_after_definite_failures_and_blocks(self):
        event = self.event()
        ids = [self.intent(event, action=name) for name in ("mute", "reply", "recall")]
        executor = self.FakeExecutor(self, ["failed", "blocked", "succeeded"])
        result = asyncio.run(self.host(executor).confirm_rule_actions(**self.rule_scope()))
        self.assertEqual([item["status"] for item in result["actions"]], ["failed", "blocked", "succeeded"])
        self.assertEqual([item["intent_id"] for item in result["actions"]], ids)
        self.assertFalse(result["stopped"])
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(len(executor.calls), 3)

    def test_unknown_or_review_result_stops_and_keeps_the_rest_waiting(self):
        for stop_status in ("unknown", "review"):
            with self.subTest(stop_status=stop_status):
                with closing(sqlite3.connect(self.path)) as db:
                    for table in ("execution_intents", "dry_run_events", "review_closures"):
                        db.execute(f"DELETE FROM {table}")
                    db.commit()
                event = self.event()
                first = self.intent(event, action="mute")
                second = self.intent(event, action="reply")
                third = self.intent(event, action="recall")
                executor = self.FakeExecutor(self, ["succeeded", stop_status, "succeeded"])
                host = self.host(executor)
                result = asyncio.run(host.confirm_rule_actions(**self.rule_scope()))
                self.assertEqual(result["actions"], [{"intent_id": first, "status": "succeeded"},
                                                     {"intent_id": second, "status": stop_status}])
                self.assertTrue(result["stopped"])
                self.assertEqual(result["remaining"], 1)
                self.assertEqual(len(executor.calls), 2)
                self.assertEqual(self.statuses()[third], "pending_confirm")
                review = self.queue.pending_reviews("p", "g")
                self.assertEqual([action["id"] for action in review["messages"][0]["actions"]], [third])
                self.assertEqual(review["total_actions"], 1)
                self.assertEqual(self.queue.pending_rule_actions(**self.rule_scope()), [third])
                executor.outcomes = ["succeeded"]
                self.assertEqual(asyncio.run(host.confirm_rule_actions(**self.rule_scope())),
                                 {"actions": [{"intent_id": third, "status": "succeeded"}],
                                  "stopped": False, "remaining": 0})
                self.assertEqual(self.queue.pending_reviews("p", "g")["total_actions"], 0)

    def test_rule_confirmation_with_nothing_pending_returns_empty_without_executing(self):
        event = self.event()
        self.intent(event, action="recall", status="pending_auto")
        self.intent(event, rule="b")
        executor = self.FakeExecutor(self, ["succeeded"])
        self.assertEqual(asyncio.run(self.host(executor).confirm_rule_actions(**self.rule_scope())),
                         {"actions": [], "stopped": False, "remaining": 0})
        self.assertEqual(executor.calls, [])
        empty = self.queue.pending_rule_actions(**self.rule_scope(rule_id="missing"))
        self.assertEqual(empty, [])

    def test_executor_exception_is_reported_as_unknown_and_stops(self):
        event = self.event()
        first = self.intent(event, action="mute")
        second = self.intent(event, action="reply")
        executor = self.FakeExecutor(self, [], raises=True)
        host = self.host(executor)
        with patch.dict(sys.modules, {"astrbot": types.ModuleType("astrbot"),
                                     "astrbot.api": types.SimpleNamespace(logger=types.SimpleNamespace(
                                         error=lambda *args: None))}):
            result = asyncio.run(host.confirm_rule_actions(**self.rule_scope()))
        await_tasks(host)
        self.assertEqual(result["actions"], [{"intent_id": first, "status": "unknown"}])
        self.assertTrue(result["stopped"])
        self.assertEqual(result["remaining"], 2)
        self.assertEqual(self.statuses(), {first: "pending_confirm", second: "pending_confirm"})

    def test_rule_confirmation_skips_actions_that_are_no_longer_waiting(self):
        event = self.event()
        dismissed = self.intent(event, action="mute")
        done = self.intent(event, action="reply")
        remaining = self.intent(event, action="recall")
        self.queue.close_review_item(target="action", actor="admin", intent_id=dismissed)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE execution_intents SET status='succeeded' WHERE id=?", (done,))
            db.commit()
        executor = self.FakeExecutor(self, ["succeeded"])
        result = asyncio.run(self.host(executor).confirm_rule_actions(**self.rule_scope()))
        self.assertEqual(result["actions"], [{"intent_id": remaining, "status": "succeeded"}])
        self.assertEqual([call[0] for call in executor.calls], [remaining])

    def test_closed_host_refuses_rule_confirmation(self):
        event = self.event()
        self.intent(event, action="mute")
        executor = self.FakeExecutor(self, ["succeeded"])
        host = self.host(executor)
        host.closed = True
        with self.assertRaises(RuntimeError):
            asyncio.run(host.confirm_rule_actions(**self.rule_scope()))
        self.assertEqual(executor.calls, [])

    # ManagementService

    def test_close_review_item_accepts_a_rule_scope_with_exact_fields(self):
        event = self.event()
        mute = self.intent(event, action="mute")
        reply = self.intent(event, action="reply")
        other = self.intent(event, rule="b")
        result = self.service.close_review_item("admin", {"target": "rule_actions", **self.rule_scope()})
        self.assertEqual((result["closed"], result["already_closed"]), (2, 0))
        self.assertEqual(self.statuses(), {mute: "dismissed", reply: "dismissed", other: "pending_confirm"})
        for body in ({"target": "rule_actions", **self.rule_scope(), "intent_id": mute},
                     {"target": "rule_actions", **self.rule_scope(), "event_id": event},
                     {"target": "rule_actions", "platform_id": "p", "group_id": "g", "message_id": "m"},
                     {"target": "rule_actions", **self.rule_scope(rule_id="")},
                     {"target": "rule_actions", **self.rule_scope(rule_id="x" * 513)},
                     {"target": "rule_actions", **self.rule_scope(), "kind": "checked"}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.service.close_review_item("admin", body)
        for actor in (None, "", " ", "api_key:key"):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                self.service.close_review_item(actor, {"target": "rule_actions", **self.rule_scope()})
        self.assertEqual(self.closures(), self.closures())

    def test_close_review_item_rule_scope_is_checked_for_the_group(self):
        class Restricted(ManagementService):
            def _authorized(self, actor, platform_id, group_id):
                return super()._authorized(actor, platform_id, group_id) and group_id == "g"

        service = Restricted(None, self.queue)
        event = self.event()
        self.intent(event, rule="b")
        with self.assertRaises(PermissionError):
            service.close_review_item("admin", {"target": "rule_actions", **self.rule_scope(group_id="other")})
        with self.assertRaises(PermissionError):
            service.rule_confirmation_scope("admin", self.rule_scope(group_id="other"))
        self.assertEqual(self.statuses(), {1: "pending_confirm"})

    def test_rule_confirmation_scope_validates_and_authorizes(self):
        scope = self.rule_scope()
        self.assertEqual(self.service.rule_confirmation_scope("admin", scope), scope)
        self.assertEqual(self.service.rule_confirmation_scope("member", scope), scope)
        for body in ({}, {"platform_id": "p", "group_id": "g", "message_id": "m"},
                     {**scope, "intent_id": 1}, {**scope, "rule_id": ""}, {**scope, "group_id": " "},
                     {**scope, "message_id": None}, {**scope, "message_id": 1}, {**scope, "message_id": True},
                     {**scope, "rule_id": "x" * 513}, {**scope, "platform_id": "p" * 513}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.service.rule_confirmation_scope("admin", body)
        for actor in (None, "", " ", "api_key:key"):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                self.service.rule_confirmation_scope(actor, scope)
        self.assertEqual(self.service.rule_confirmation_scope("admin", self.rule_scope(message_id="x" * 512))["message_id"],
                         "x" * 512)
        self.assertEqual(json.dumps(scope), json.dumps(self.rule_scope()))


def await_tasks(host):
    """Drain the host's fire-and-forget tasks so test cleanup is deterministic."""

    async def drain():
        await asyncio.gather(*list(host._mute_tasks), return_exceptions=True)
    asyncio.run(drain())


if __name__ == "__main__":
    unittest.main()
