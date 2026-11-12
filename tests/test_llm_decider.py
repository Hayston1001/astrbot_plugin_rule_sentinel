import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from sentinel.dry_run_queue import DryRunQueue
from sentinel.dry_run_worker import DryRunWorker
from sentinel.llm_decider import LlmDecider
from sentinel.rule_store import RuleStore


class LlmDeciderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "state.sqlite"
        self.rules = RuleStore(self.path)
        self.rules.initialize()
        self.queue = DryRunQueue(self.path)
        self.queue.initialize()
        self.rule = dict(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                         text="No spam", enabled=True, allowed_actions=["mute"],
                         duration_mode="strict", severity_seconds={"low": 60}, modified_by="admin")
        self.rules.save(**self.rule)
        self.queue.enqueue(platform_id="p", group_id="g", message_id="m", capacity=10,
                           payload={"text": "private message", "sender_id": "u"})

    def audit(self):
        with closing(sqlite3.connect(self.path)) as db:
            status, detail = db.execute("SELECT status, detail FROM dry_run_events ORDER BY id DESC LIMIT 1").fetchone()
            return status, json.loads(detail)

    async def test_snapshot_and_model_reason_are_audited(self):
        async def invoke(**kwargs):
            self.assertNotIn("modified_by", kwargs["rules"][0])
            self.assertNotIn("group_id", kwargs["rules"][0])
            self.rules.save(**{**self.rule, "expected_version": 1, "enabled": False})
            return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["propose_action"],
                tools_call_args=[{"violated": True, "rule_id": "r", "severity": "low", "reason": "PRIVATE\u200b  MESSAGE"}])
        worker = DryRunWorker(self.queue, LlmDecider(self.rules, invoke, model="llm", max_duration_seconds=300, queue=self.queue), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, audit = self.audit()
        self.assertEqual(status, "review")
        self.assertEqual(audit["review_reason"], "rule_snapshot_changed")
        self.assertEqual(audit["proposal"]["rule_version"], 1)
        self.assertEqual(audit["proposal"]["target_sender_id"], "u")
        self.assertEqual(audit["proposal"]["reason"], "PRIVATE\u200b  MESSAGE")
        self.assertGreaterEqual(audit["model_latency_ms"], 0)
        self.assertNotIn("reason_redacted", audit)

    async def test_unknown_rule_is_rejected(self):
        async def invoke(**kwargs):
            return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["propose_action"],
                tools_call_args=[{"violated": True, "rule_id": "other", "severity": "low", "reason": "reason"}])
        worker = DryRunWorker(self.queue, LlmDecider(self.rules, invoke, model="llm", max_duration_seconds=300, queue=self.queue), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, audit = self.audit()
        self.assertEqual(status, "rejected")
        self.assertEqual(audit["validation"], "unknown_rule")

    async def test_forbidden_action_is_rejected_without_mute_fallback(self):
        async def invoke(**kwargs):
            return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["propose_action"],
                tools_call_args=[{"action": "reply", "violated": True, "rule_id": "r", "reason": "rule violated"}])
        worker = DryRunWorker(self.queue, LlmDecider(self.rules, invoke, model="llm", max_duration_seconds=300, queue=self.queue), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, audit = self.audit()
        self.assertEqual(status, "rejected")
        self.assertEqual(audit["response_status"], "invalid_verdict")
        self.assertNotIn("proposal", audit)

    async def test_language_is_audited_and_summary_reason_is_preserved(self):
        self.rules.save(**{**self.rule, "expected_version": 1, "text": "禁止发布广告"})
        async def invoke(**kwargs):
            self.assertEqual(kwargs["language"], "zh")
            return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["propose_action"],
                tools_call_args=[{"violated": True, "rule_id": "r", "severity": "low", "reason": "发布了推广内容"}])
        worker = DryRunWorker(self.queue, LlmDecider(self.rules, invoke, model="llm", max_duration_seconds=300,
            queue=self.queue), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        events = self.queue.list_events(platform_id="p", group_id="g")
        self.assertEqual([event["status"] for event in events], ["started", "proposed"])
        start, result = [json.loads(event["detail"]) for event in events]
        self.assertEqual(start["prompt_language"], "zh")
        self.assertEqual(result["prompt_language"], "zh")
        self.assertEqual(result["proposal"]["reason"], "发布了推广内容")
        self.assertNotIn("reason_redacted", result)

    async def test_language_is_audited_when_model_call_fails(self):
        async def invoke(**kwargs):
            raise RuntimeError("private provider error")
        worker = DryRunWorker(self.queue, LlmDecider(self.rules, invoke, model="llm", max_duration_seconds=300,
            queue=self.queue), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        events = self.queue.list_events(platform_id="p", group_id="g")
        self.assertEqual([event["status"] for event in events], ["started", "failed"])
        self.assertEqual(json.loads(events[0]["detail"])["prompt_language"], "en")
        self.assertEqual(json.loads(events[1]["detail"]), {"prompt_language": "en", "reason": "decision_error"})

    async def test_no_action_reason_is_recorded_as_returned(self):
        for text, expected in (("NO_ACTION\nNo rule was violated.", "No rule was violated."),
                               ("NO_ACTION\nPRIVATE\u200b  MESSAGE", "PRIVATE\u200b  MESSAGE"),
                               ("NO_ACTION", None)):
            with self.subTest(text=text):
                self.queue.enqueue(platform_id="p", group_id="g", message_id=text, capacity=10,
                                   payload={"text": "private message", "sender_id": "u"})
                item = self.queue.claim("p", "g")
                if item.message_id == "m":
                    self.queue.finish(item, status="skipped", detail="setup")
                    item = self.queue.claim("p", "g")
                async def invoke(**kwargs):
                    return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=[], tools_call_args=[],
                                           completion_text=text)
                result = await LlmDecider(self.rules, invoke, model="llm", max_duration_seconds=300, queue=self.queue)(item)
                detail = json.loads(result.detail)
                self.assertEqual(result.status, "no_action")
                self.assertEqual(detail.get("reason"), expected)
                self.assertNotIn("reason_redacted", detail)
                self.queue.finish(item, status=result.status, detail=result.detail, rule_versions=result.rule_versions)

    async def test_priority_no_action_reasons_are_recorded_per_rule(self):
        self.rules.save(**{**self.rule, "rule_id": "r2"})
        async def invoke(**kwargs):
            return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["assess_rules"],
                tools_call_args=[{"assessments": [{"rule_id": "r", "outcome": "no_hit", "reason": "No spam."},
                                                   {"rule_id": "r2", "outcome": "no_hit", "reason": "private message"}]}])
        worker = DryRunWorker(self.queue, LlmDecider(self.rules, invoke, model="llm", max_duration_seconds=300,
            queue=self.queue, strategy="priority"), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, audit = self.audit()
        self.assertEqual(status, "no_action")
        self.assertEqual(audit["reason"], "r: No spam.; r2: private message")
        self.assertNotIn("reason_redacted", audit)

    async def test_disabled_no_action_reasons_are_discarded_for_both_strategies(self):
        for index, strategy in enumerate(("model", "priority")):
            with self.subTest(strategy=strategy):
                if index:
                    self.queue.enqueue(platform_id="p", group_id="g", message_id="second", capacity=10,
                                       payload={"text": "private message", "sender_id": "u"})
                item = self.queue.claim("p", "g")
                async def invoke(**kwargs):
                    if strategy == "model":
                        return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=[], tools_call_args=[],
                                               completion_text="NO_ACTION\nUnrequested reason")
                    return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["assess_rules"],
                        tools_call_args=[{"assessments": [{"rule_id": "r", "outcome": "no_hit", "reason": "Unrequested reason"}]}])
                result = await LlmDecider(self.rules, invoke, model="llm", max_duration_seconds=300, queue=self.queue,
                                          strategy=strategy, no_action_reason_enabled=False)(item)
                detail = json.loads(result.detail)
                self.assertEqual(result.status, "no_action")
                self.assertFalse(detail["no_action_reason_enabled"])
                self.assertNotIn("reason", detail)
                self.assertNotIn("Unrequested reason", result.detail)
                self.assertTrue(self.queue.finish(item, status=result.status, detail=result.detail, rule_versions=result.rule_versions))

    async def test_disabled_no_action_reason_keeps_violation_reason(self):
        for index, strategy in enumerate(("model", "priority")):
            with self.subTest(strategy=strategy):
                if index:
                    self.queue.enqueue(platform_id="p", group_id="g", message_id="second", capacity=10,
                                       payload={"text": "private message", "sender_id": "u"})
                item = self.queue.claim("p", "g")
                async def invoke(**kwargs):
                    call = {"rule_id": "r", "severity": "low", "reason": "The rule was violated"}
                    if strategy == "model":
                        return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["propose_action"],
                                               tools_call_args=[{**call, "violated": True}])
                    return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["assess_rules"],
                                           tools_call_args=[{"assessments": [{**call, "outcome": "hit"}]}])
                result = await LlmDecider(self.rules, invoke, model="llm", max_duration_seconds=300, queue=self.queue,
                                          strategy=strategy, no_action_reason_enabled=False)(item)
                self.assertEqual(result.status, "proposed")
                self.assertEqual(json.loads(result.detail)["proposal"]["reason"], "The rule was violated")
                self.assertTrue(self.queue.finish(item, status=result.status, detail=result.detail, rule_versions=result.rule_versions))

    async def test_priority_batch_selects_highest_matched_rule(self):
        self.rules.save(**{**self.rule, "rule_id": "r2", "priority": 10})
        async def invoke(**kwargs):
            self.assertEqual(kwargs["strategy"], "priority")
            return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["assess_rules"],
                tools_call_args=[{"assessments": [
                    {"rule_id": "r", "outcome": "hit", "severity": "low", "reason": "reason one"},
                    {"rule_id": "r2", "outcome": "hit", "severity": "low", "reason": "reason two"}]}])
        worker = DryRunWorker(self.queue, LlmDecider(self.rules, invoke, model="llm", max_duration_seconds=300,
            queue=self.queue, strategy="priority"), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, audit = self.audit()
        self.assertEqual(status, "proposed")
        self.assertEqual(audit["proposal"]["rule_id"], "r2")
        self.assertEqual(audit["selection_reason"], "highest_priority_match")

    async def test_priority_uses_rule_language_and_preserves_summary(self):
        self.rules.save(**{**self.rule, "expected_version": 1, "text": "禁止发布广告"})
        async def invoke(**kwargs):
            self.assertEqual((kwargs["strategy"], kwargs["language"]), ("priority", "zh"))
            return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["assess_rules"],
                tools_call_args=[{"assessments": [
                    {"rule_id": "r", "outcome": "hit", "severity": "low", "reason": "发布了推广内容"}]}])
        worker = DryRunWorker(self.queue, LlmDecider(self.rules, invoke, model="llm", max_duration_seconds=300,
            queue=self.queue, strategy="priority"), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        events = self.queue.list_events(platform_id="p", group_id="g")
        self.assertEqual([json.loads(event["detail"])["prompt_language"] for event in events], ["zh", "zh"])
        self.assertEqual(json.loads(events[-1]["detail"])["proposal"]["reason"], "发布了推广内容")

    async def test_priority_tie_and_incomplete_batch_require_review(self):
        self.rules.save(**{**self.rule, "rule_id": "r2"})
        async def invoke(**kwargs):
            return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["assess_rules"],
                tools_call_args=[{"assessments": [
                    {"rule_id": "r", "outcome": "hit", "severity": "low", "reason": "reason one"},
                    {"rule_id": "r2", "outcome": "hit", "severity": "low", "reason": "reason two"}]}])
        worker = DryRunWorker(self.queue, LlmDecider(self.rules, invoke, model="llm", max_duration_seconds=300,
            queue=self.queue, strategy="priority"), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, audit = self.audit()
        self.assertEqual(status, "review")
        self.assertEqual(audit["selection_reason"], "equal_priority_conflict")
        self.assertNotIn("proposal", audit)

    async def test_priority_missing_rule_requires_review(self):
        self.rules.save(**{**self.rule, "rule_id": "r2", "priority": 10})
        async def invoke(**kwargs):
            return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["assess_rules"],
                tools_call_args=[{"assessments": [{"rule_id": "r", "outcome": "no_hit"}]}])
        worker = DryRunWorker(self.queue, LlmDecider(self.rules, invoke, model="llm", max_duration_seconds=300,
            queue=self.queue, strategy="priority"), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, audit = self.audit()
        self.assertEqual(status, "review")
        self.assertEqual(audit["selection_reason"], "incomplete_or_duplicate_assessment")

    async def test_priority_does_not_ignore_invalid_lower_priority_hit(self):
        self.rules.save(**{**self.rule, "rule_id": "r2", "priority": 10})
        async def invoke(**kwargs):
            return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["assess_rules"],
                tools_call_args=[{"assessments": [
                    {"rule_id": "r", "outcome": "hit", "severity": "unknown", "reason": "reason one"},
                    {"rule_id": "r2", "outcome": "hit", "severity": "low", "reason": "reason two"}]}])
        worker = DryRunWorker(self.queue, LlmDecider(self.rules, invoke, model="llm", max_duration_seconds=300,
            queue=self.queue, strategy="priority"), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, audit = self.audit()
        self.assertEqual(status, "rejected")
        self.assertEqual(audit["validation"], "invalid_severity")
        self.assertNotIn("proposal", audit)
