import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sentinel.dry_run_queue import DryRunQueue
from sentinel.group_settings import decision_fingerprint
from sentinel.mute_execution_store import MuteExecutionStore
from sentinel.mute_executor import MuteExecutor
from sentinel.rule_store import RuleStore
from sentinel.settings_store import SettingsStore

MUTE = {"type": "mute", "duration_mode": "strict", "severity_seconds": {"low": 60}}
REPLY = {"type": "reply", "template": "notice {rule_id}"}
RECALL = {"type": "recall"}


class EqualPriorityIntentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.sqlite"
        self.rules, self.queue = RuleStore(self.path), DryRunQueue(self.path)
        self.rules.initialize()
        self.queue.initialize()
        self.settings = SettingsStore(self.path, Path(temporary.name) / "backup")
        self.config = self.settings.load_or_migrate({})
        self.config.update(monitoring_groups=["p/g"], action_execution_enabled=True,
                           decision_backend="llm", llm_provider_id="model")
        self.settings.save(self.config)
        self.counter = 0

    def save_rule(self, rule_id, mode, actions, priority=0):
        self.rules.save(platform_id="p", group_id="g", rule_id=rule_id, expected_version=0, text=rule_id,
                        enabled=True, execution_mode=mode, actions=actions, priority=priority, modified_by="admin")

    def proposal(self, rule_id, version=1):
        return {"rule_id": rule_id, "rule_version": version, "target_sender_id": "sender", "duration_seconds": 60}

    def decide(self, rule_ids, *, peer_tie=False, proposals=None, status="proposed", **finish):
        self.counter += 1
        self.queue.enqueue(platform_id="p", group_id="g", message_id=f"m{self.counter}", capacity=10,
                           payload={"text": "private", "sender_id": "sender"})
        item = self.queue.claim("p", "g")
        proposals = proposals or [self.proposal(rule_id) for rule_id in rule_ids]
        detail = {"proposals": proposals, "proposal": proposals[0], "selected_rules": rule_ids, "peer_tie": peer_tie}
        versions = tuple((rule.rule_id, rule.version) for rule in self.rules.current_for_group("p", "g") if rule.enabled)
        return item, self.queue.finish(item, status=status, detail=json.dumps(detail), rule_versions=versions, **finish)

    def intents(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("""SELECT rule_id, action, mode, mode_reason, status, plan_id, action_index
                FROM execution_intents ORDER BY id""").fetchall()

    def test_three_gate_matrix(self):
        self.save_rule("a", "auto", [MUTE])
        self.save_rule("b", "auto", [MUTE])
        self.save_rule("c", "confirm", [MUTE])
        cases = (
            ("execute all, auto rules", ["a", "b"], False, [("auto", None, "pending_auto")] * 2),
            ("equal priority confirm, auto rules", ["a", "b"], True, [("confirm", "peer_tie", "pending_confirm")] * 2),
            ("equal priority confirm, manual rule keeps the rule reason", ["c"], True, [("confirm", "rule", "pending_confirm")]),
            ("execute all, manual rule", ["c"], False, [("confirm", "rule", "pending_confirm")]),
            ("tie with a manual and an auto rule", ["a", "c"], True,
             [("confirm", "peer_tie", "pending_confirm"), ("confirm", "rule", "pending_confirm")]),
        )
        for name, rule_ids, tie, expected in cases:
            with self.subTest(name):
                with closing(sqlite3.connect(self.path)) as db:
                    db.execute("DELETE FROM execution_intents")
                    db.commit()
                self.decide(rule_ids, peer_tie=tie)
                self.assertEqual([row[2:5] for row in self.intents()], expected)

    def test_global_execution_off_only_records(self):
        self.save_rule("a", "auto", [MUTE])
        self.save_rule("c", "confirm", [MUTE])
        self.settings.save({**self.config, "action_execution_enabled": False})
        self.decide(["a", "c"], peer_tie=True)
        self.assertEqual([(row[4]) for row in self.intents()], ["inactive", "inactive"])
        self.assertEqual([row[2:4] for row in self.intents()], [("confirm", "peer_tie"), ("confirm", "rule")])
        self.assertEqual(self.queue.list_messages(platform_id="p", group_id="g")[0]["execution_summary"], "inactive")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM execution_intents WHERE status='pending_confirm'").fetchone()[0], 0)

    def test_group_record_mode_only_records_even_with_manual_peer_tie(self):
        self.save_rule("a", "auto", [MUTE])
        self.save_rule("b", "auto", [MUTE])
        self.config["group_overrides"] = {"p/g": {"execution_mode": "record"}}
        self.settings.save(self.config)
        self.decide(["a", "b"], peer_tie=True)
        self.assertEqual({row[4] for row in self.intents()}, {"inactive"})

    def test_same_plan_can_hold_automatic_and_manual_rules(self):
        self.save_rule("a", "auto", [RECALL, MUTE])
        self.save_rule("c", "confirm", [REPLY, MUTE])
        self.decide(["a", "c"])
        rows = self.intents()
        self.assertEqual([(row[0], row[1], row[4], row[6]) for row in rows],
                         [("a", "recall", "pending_auto", 0), ("a", "mute", "pending_auto", 1),
                          ("c", "reply", "pending_confirm", 2), ("c", "mute", "pending_confirm", 3)])
        self.assertEqual(len({row[5] for row in rows}), 1)
        self.assertEqual(self.queue.list_messages(platform_id="p", group_id="g")[0]["execution_summary"], "pending_confirm")

    def test_order_is_rule_order_then_action_order(self):
        self.save_rule("a", "auto", [MUTE, RECALL])
        self.save_rule("b", "auto", [REPLY, MUTE, RECALL])
        self.decide(["b", "a"])
        self.assertEqual([(row[0], row[1], row[6]) for row in self.intents()],
                         [("b", "reply", 0), ("b", "mute", 1), ("b", "recall", 2), ("a", "mute", 3), ("a", "recall", 4)])

    def test_inherited_rule_follows_the_global_default(self):
        self.save_rule("i", "inherit", [MUTE])
        for default, expected in (("auto", ("auto", None, "pending_auto")), ("confirm", ("confirm", "rule", "pending_confirm"))):
            with self.subTest(default=default):
                with closing(sqlite3.connect(self.path)) as db:
                    db.execute("DELETE FROM execution_intents")
                    db.commit()
                self.config["default_action_mode"] = default
                self.settings.save(self.config)
                self.decide(["i"], peer_tie=False)
                self.assertEqual(self.intents()[0][2:5], expected)

    def test_inherited_rule_with_auto_default_reports_peer_tie(self):
        self.save_rule("i", "inherit", [MUTE])
        self.decide(["i"], peer_tie=True)
        self.assertEqual(self.intents()[0][2:5], ("confirm", "peer_tie", "pending_confirm"))

    def test_a_rule_never_appears_twice_in_one_plan(self):
        self.save_rule("a", "auto", [MUTE])
        self.save_rule("b", "auto", [MUTE])
        with self.assertRaises(ValueError):
            self.decide(["a", "a"])
        self.assertEqual(self.intents(), [])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT state FROM dry_run_queue ORDER BY rowid DESC").fetchone()[0], "processing")
            self.assertEqual(db.execute("SELECT count(*) FROM dry_run_events WHERE status='proposed'").fetchone()[0], 0)

    def test_every_selected_rule_is_checked_against_the_trusted_snapshot(self):
        self.save_rule("a", "auto", [MUTE])
        self.save_rule("b", "auto", [MUTE])
        bad = (
            [self.proposal("a"), {**self.proposal("b"), "target_sender_id": "other"}],
            [self.proposal("a"), self.proposal("b", version=2)],
            [self.proposal("a"), self.proposal("unknown")],
            [self.proposal("a"), {**self.proposal("b"), "duration_seconds": 61}],
        )
        for proposals in bad:
            with self.subTest(proposals=proposals), self.assertRaises(ValueError):
                self.decide(["a", "b"], proposals=proposals)
            with closing(sqlite3.connect(self.path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM execution_intents").fetchone()[0], 0)
                db.execute("UPDATE dry_run_queue SET state='done', payload=NULL, lease=NULL")
                db.commit()

    def test_action_missing_from_the_rule_is_rejected(self):
        self.save_rule("a", "auto", [RECALL])
        with self.assertRaises(ValueError):
            self.decide(["a"], proposals=[{**self.proposal("a"), "action": "mute"}])

    def test_legacy_single_proposal_records_still_create_intents(self):
        self.save_rule("a", "auto", [MUTE])
        self.counter += 1
        self.queue.enqueue(platform_id="p", group_id="g", message_id="legacy", capacity=10,
                           payload={"text": "private", "sender_id": "sender"})
        item = self.queue.claim("p", "g")
        detail = json.dumps({"proposal": self.proposal("a")})
        self.assertTrue(self.queue.finish(item, status="proposed", detail=detail, rule_versions=(("a", 1),)))
        self.assertEqual([row[2:5] for row in self.intents()], [("auto", None, "pending_auto")])

    def test_policy_changed_during_decision_requires_review(self):
        self.save_rule("a", "auto", [MUTE])
        self.save_rule("b", "auto", [MUTE])
        fingerprint = decision_fingerprint(self.config, "p", "g")
        self.settings.save({**self.config, "equal_priority_policy": "confirm"})
        self.decide(["a", "b"], settings_fingerprint=fingerprint)
        self.assertEqual(self.intents(), [])
        with closing(sqlite3.connect(self.path)) as db:
            status, detail = db.execute("SELECT status, detail FROM dry_run_events ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(status, "review")
        self.assertEqual(json.loads(detail)["review_reason"], "group_settings_changed_at_commit")

    def test_rule_mode_changed_after_the_decision_is_rechecked_before_executing(self):
        self.save_rule("a", "auto", [MUTE])
        self.save_rule("b", "auto", [MUTE])
        self.decide(["a", "b"])
        self.rules.save(platform_id="p", group_id="g", rule_id="a", expected_version=1, text="a", enabled=True,
                        execution_mode="confirm", actions=[MUTE], modified_by="admin")
        store = MuteExecutionStore(self.path)
        self.assertIsNone(store.begin_attempt(1, requested_mode="auto"))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM execution_intents WHERE id=1").fetchone()[0], "review")
            self.assertEqual(db.execute("SELECT status FROM execution_intents WHERE id=2").fetchone()[0], "pending_auto")

    def test_manual_intent_stays_valid_when_the_rule_is_automatic(self):
        self.save_rule("a", "auto", [MUTE])
        self.save_rule("b", "auto", [MUTE])
        self.decide(["a", "b"], peer_tie=True)
        calls = []

        class Platform:
            async def verify_permission(self, *args):
                return True

            async def mute(self, *args):
                calls.append(args)
                return {"outcome": "succeeded"}

        executor = MuteExecutor(MuteExecutionStore(self.path), Platform())
        self.assertEqual(asyncio.run(executor.execute(1, mode="auto")), "not_claimed")
        self.assertEqual(asyncio.run(executor.execute(1, mode="confirm")), "succeeded")
        self.assertEqual(len(calls), 1)
        self.assertEqual([row[4] for row in self.intents()], ["succeeded", "pending_confirm"])


if __name__ == "__main__":
    unittest.main()
