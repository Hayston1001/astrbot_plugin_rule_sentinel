import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sentinel.dry_run_queue import DryRunQueue
from sentinel.dry_run_worker import DryRunWorker
from sentinel.jev_decider import JevDecider
from sentinel.rule_store import RuleStore


class JevDeciderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "state.sqlite"
        self.rules = RuleStore(self.path)
        self.rules.initialize()
        self.queue = DryRunQueue(self.path)
        self.queue.initialize()
        self.args = dict(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                         text="No spam", enabled=True, allowed_actions=["mute"],
                         duration_mode="strict", severity_seconds={"low": 60}, modified_by="admin")

    def enqueue(self):
        self.queue.enqueue(platform_id="p", group_id="g", message_id="m", capacity=10,
                           payload={"text": "private group content", "sender_id": "u"})

    def audit(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT status, detail FROM dry_run_events ORDER BY id DESC LIMIT 1").fetchone()

    async def test_snapshot_to_validated_proposal_and_audit(self):
        self.rules.save(**self.args)
        self.enqueue()
        async def send(request):
            self.assertEqual(request["state"]["message_untrusted"], "private group content")
            return {"model": "resolved", "answers": {"decision": {"type": "choice", "choice": "r_hit_low_0", "confidence": 0.8, "probabilities": {"no_action": 0.1, "review": 0.1, "r_hit_low_0": 0.8}}}}
        worker = DryRunWorker(self.queue, JevDecider(self.rules, send, model="configured", max_duration_seconds=300, queue=self.queue), concurrency=1, timeout_seconds=2)
        self.assertEqual(await worker.drain_group("p", "g"), 1)
        status, raw = self.audit()
        audit = json.loads(raw)
        self.assertEqual(status, "proposed")
        self.assertEqual(audit["rules"][0]["version"], 1)
        self.assertEqual(audit["proposal"]["target_sender_id"], "u")
        self.assertEqual(audit["candidate_options"]["r_hit_low_0"], {"rule_id": "r", "rule_version": 1, "severity": "low", "duration_seconds": 60})
        self.assertEqual(audit["choice"], "r_hit_low_0")
        started = self.queue.list_events(platform_id="p", group_id="g")[0]
        self.assertEqual(json.loads(started["detail"])["candidate_options"]["decision"], audit["candidate_options"])
        self.assertGreaterEqual(audit["model_latency_ms"], 0)
        self.assertIsNone(audit["reason"])
        self.assertNotIn("private group content", raw)

    async def test_reply_action_is_selected_from_rule(self):
        fields = {key: value for key, value in self.args.items() if key not in ("allowed_actions", "duration_mode", "severity_seconds")}
        self.rules.save(**fields, actions=[{"type": "reply", "mode": "confirm", "template": "{sender_id}"}])
        self.enqueue()
        async def send(request):
            self.assertEqual(request["questions"]["decision"]["criteria"]["r_hit_violation_0"]["violated"], True)
            return {"model": "resolved", "answers": {"decision": {"type": "choice", "choice": "r_hit_violation_0", "confidence": 0.8,
                    "probabilities": {"no_action": 0.1, "review": 0.1, "r_hit_violation_0": 0.8}}}}
        worker = DryRunWorker(self.queue, JevDecider(self.rules, send, model="m", max_duration_seconds=300, queue=self.queue), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, raw = self.audit()
        proposal = json.loads(raw)["proposal"]
        self.assertEqual(status, "proposed")
        self.assertNotIn("action", proposal)
        self.assertEqual(self.queue.list_messages(platform_id="p", group_id="g")[0]["executions"][0]["action"], "reply")

    async def test_unknown_jev_action_is_rejected(self):
        self.rules.save(**self.args)
        self.enqueue()
        async def send(request):
            return {"model": "resolved", "answers": {"decision": {"type": "choice", "choice": "unknown", "confidence": 0.8,
                    "probabilities": {"no_action": 0.1, "review": 0.1, "r_hit_low_0": 0.8}}}}
        worker = DryRunWorker(self.queue, JevDecider(self.rules, send, model="m", max_duration_seconds=300, queue=self.queue), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, raw = self.audit()
        self.assertEqual(status, "rejected")
        self.assertEqual(json.loads(raw)["validation"], "unknown_choice")

    async def test_rule_change_after_recheck_is_reviewed_at_audit_commit(self):
        self.rules.save(**self.args)
        self.enqueue()
        async def send(request):
            return {"model": "resolved", "answers": {"decision": {"type": "choice", "choice": "no_action",
                "confidence": 0.8, "probabilities": {"no_action": 0.8, "review": 0.1, "r_hit_low_0": 0.1}}}}
        finish = self.queue.finish
        def change_then_finish(item, **kwargs):
            self.rules.save(**{**self.args, "expected_version": 1, "text": "updated rule"})
            return finish(item, **kwargs)
        self.queue.finish = change_then_finish
        worker = DryRunWorker(self.queue, JevDecider(self.rules, send, model="m", max_duration_seconds=300,
                              queue=self.queue), concurrency=1, timeout_seconds=2)
        self.assertEqual(await worker.drain_group("p", "g"), 1)
        status, detail = self.audit()
        self.assertEqual(status, "review")
        data = json.loads(detail)
        self.assertEqual(data["review_reason"], "rule_snapshot_changed_at_commit")
        self.assertEqual(data["original_status"], "no_action")
        self.assertTrue(data["decision"]["snapshot_current"])

    async def test_multiple_rules_can_be_selected(self):
        self.rules.save(**self.args)
        self.rules.save(**{**self.args, "rule_id": "r2"})
        self.enqueue()
        async def send(request):
            return {"model": "resolved", "answers": {"decision": {"type": "choice", "choice": "r2_hit_low_1", "confidence": 0.8, "probabilities": {"no_action": 0.05, "review": 0.05, "r_hit_low_0": 0.1, "r2_hit_low_1": 0.8}}}}
        worker = DryRunWorker(self.queue, JevDecider(self.rules, send, model="m", max_duration_seconds=300, queue=self.queue), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, raw = self.audit()
        self.assertEqual(status, "proposed")
        self.assertEqual(json.loads(raw)["proposal"]["rule_id"], "r2")

    async def test_model_failure_keeps_snapshot_without_message(self):
        self.rules.save(**self.args)
        self.enqueue()
        async def send(request):
            raise RuntimeError("private group content")
        worker = DryRunWorker(self.queue, JevDecider(self.rules, send, model="m", max_duration_seconds=300, queue=self.queue), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        rows = self.queue.list_events(platform_id="p", group_id="g")
        self.assertEqual([row["status"] for row in rows], ["started", "failed"])
        self.assertEqual(json.loads(rows[0]["detail"])["rules"][0]["version"], 1)
        self.assertNotIn("private group content", json.dumps(rows))

    async def test_audit_failure_prevents_model_request(self):
        self.rules.save(**self.args)
        self.enqueue()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TRIGGER fail_start BEFORE INSERT ON dry_run_events WHEN NEW.status = 'started' BEGIN SELECT RAISE(ABORT, 'failure'); END")
            db.commit()
        called = []
        async def send(request):
            called.append(request)
        worker = DryRunWorker(self.queue, JevDecider(self.rules, send, model="m", max_duration_seconds=300, queue=self.queue), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        self.assertEqual(called, [])
        self.assertEqual(self.audit(), ("failed", "evaluation_audit_unavailable"))

    async def test_priority_uses_all_matching_rules(self):
        self.rules.save(**{**self.args, "priority": 1})
        self.rules.save(**{**self.args, "rule_id": "r2", "priority": 5})
        self.enqueue()
        async def send(request):
            self.assertEqual(set(request["questions"]), {"rule_0", "rule_1"})
            return {"model": "resolved", "answers": {name: {"type": "choice", "choice": option, "confidence": 0.8,
                "probabilities": {"no_action": 0.1, "review": 0.1, option: 0.8}} for name, option in (("rule_0", "r_hit_low_0"), ("rule_1", "r2_hit_low_0"))}}
        worker = DryRunWorker(self.queue, JevDecider(self.rules, send, model="m", max_duration_seconds=300,
            queue=self.queue, strategy="priority"), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, raw = self.audit()
        self.assertEqual(status, "proposed")
        self.assertEqual(json.loads(raw)["proposal"]["rule_id"], "r2")

    async def test_equal_priority_conflict_is_review(self):
        self.rules.save(**self.args)
        self.rules.save(**{**self.args, "rule_id": "r2"})
        self.enqueue()
        async def send(request):
            return {"model": "resolved", "answers": {name: {"type": "choice", "choice": option, "confidence": 0.8,
                "probabilities": {"no_action": 0.1, "review": 0.1, option: 0.8}} for name, option in (("rule_0", "r_hit_low_0"), ("rule_1", "r2_hit_low_0"))}}
        worker = DryRunWorker(self.queue, JevDecider(self.rules, send, model="m", max_duration_seconds=300,
            queue=self.queue, strategy="priority"), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, raw = self.audit()
        self.assertEqual(status, "review")
        detail = json.loads(raw)
        self.assertEqual(detail["selection_reason"], "equal_priority_conflict")
        self.assertEqual(detail["severity_options_by_rule"]["r"]["r_hit_low_0"], "low")

    async def test_rule_disabled_during_model_request_requires_review(self):
        self.rules.save(**self.args)
        self.enqueue()
        async def send(request):
            self.rules.save(**{**self.args, "expected_version": 1, "enabled": False})
            return {"model": "m", "answers": {"decision": {"type": "choice", "choice": "r_hit_low_0",
                "confidence": 0.8, "probabilities": {"no_action": 0.1, "review": 0.1, "r_hit_low_0": 0.8}}}}
        worker = DryRunWorker(self.queue, JevDecider(self.rules, send, model="m", max_duration_seconds=300,
            queue=self.queue), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, raw = self.audit()
        self.assertEqual(status, "review")
        detail = json.loads(raw)
        self.assertEqual(detail["review_reason"], "rule_snapshot_changed")
        self.assertEqual(detail["proposal"]["rule_version"], 1)

    async def test_untrusted_model_identifier_cannot_echo_message_into_audit(self):
        self.rules.save(**self.args)
        self.enqueue()
        async def send(request):
            return {"model": "private group content", "answers": {"decision": {"type": "choice", "choice": "r_hit_low_0",
                "confidence": 0.8, "probabilities": {"no_action": 0.1, "review": 0.1, "r_hit_low_0": 0.8}}}}
        worker = DryRunWorker(self.queue, JevDecider(self.rules, send, model="requested", max_duration_seconds=300,
            queue=self.queue), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, raw = self.audit()
        self.assertEqual(status, "proposed")
        self.assertIsNone(json.loads(raw)["model"])
        self.assertNotIn("private group content", raw)

    async def test_oversized_rule_snapshot_fails_without_model_or_truncation(self):
        self.rules.save(**{**self.args, "text": "x" * 100001})
        self.enqueue()
        called = []
        async def send(request):
            called.append(True)
        worker = DryRunWorker(self.queue, JevDecider(self.rules, send, model="m", max_duration_seconds=300,
            queue=self.queue), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        self.assertEqual(called, [])
        self.assertEqual(self.audit(), ("failed", "rule_snapshot_too_large"))
