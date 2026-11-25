import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from sentinel.dry_run_queue import DryRunQueue
from sentinel.dry_run_worker import DryRunWorker
from sentinel.image_fetch import FetchedImage, ImageFetchError
from sentinel.image_prefetch import PrefetchedImages
from sentinel.llm_decider import LlmDecider
from sentinel.rule_store import RuleStore
from sentinel.settings_store import SettingsStore

IMAGE = FetchedImage("data:image/png;base64,QUJD", 3)
IMAGE_WITH_PREVIEW = FetchedImage("data:image/png;base64,QUJD", 3,
                                  ("image/jpeg", b"\xff\xd8\xffpreview"))
IMAGE_URL = "https://cdn.example.com/a.png"


class Fetcher:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def fetch(self, urls):
        self.calls.append(tuple(urls))
        if self.error is not None:
            raise self.error
        return self.result or (IMAGE,)


class Prefetcher:
    def __init__(self, entry=None):
        self.entry = entry
        self.keys = []

    async def consume(self, key):
        self.keys.append(key)
        return self.entry


def assess(*items, **extra):
    return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["assess_rules"],
                           tools_call_args=[{"assessments": list(items)}], **extra)


def hit(rule_id="r", severity="low", reason="spam", **extra):
    item = {"rule_id": rule_id, "outcome": "hit", "reason": reason}
    if severity is not None:
        item["severity"] = severity
    return {**item, **extra}


def no_hit(rule_id="r", **extra):
    return {"rule_id": rule_id, "outcome": "no_hit", **extra}


class ImageInvoker:
    def __init__(self, *, caption=None, caption_error=None):
        self.calls = []
        self.caption_calls = []
        self.caption_text = caption
        self.caption_error = caption_error

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return assess(hit())

    async def caption(self, **kwargs):
        self.caption_calls.append(kwargs)
        if self.caption_error is not None:
            raise self.caption_error
        return SimpleNamespace(completion_text=self.caption_text)


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

    def decider(self, invoke, **kwargs):
        return LlmDecider(self.rules, invoke, model="llm", max_duration_seconds=300, queue=self.queue, **kwargs)

    async def run_worker(self, invoke, **kwargs):
        worker = DryRunWorker(self.queue, self.decider(invoke, **kwargs), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        return self.audit()

    def add_rule(self, rule_id, **extra):
        self.rules.save(**{**self.rule, "rule_id": rule_id, "expected_version": 0, **extra})

    async def test_snapshot_and_model_reason_are_audited(self):
        async def invoke(**kwargs):
            self.assertNotIn("modified_by", kwargs["rules"][0])
            self.assertNotIn("group_id", kwargs["rules"][0])
            self.assertNotIn("strategy", kwargs)
            self.rules.save(**{**self.rule, "expected_version": 1, "enabled": False})
            return assess(hit(reason="PRIVATE\u200b  MESSAGE"))
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "review")
        self.assertEqual(audit["review_reason"], "rule_snapshot_changed")
        self.assertEqual(audit["proposal"]["rule_version"], 1)
        self.assertEqual(audit["proposal"]["target_sender_id"], "u")
        self.assertEqual(audit["proposal"]["reason"], "PRIVATE\u200b  MESSAGE")
        self.assertGreaterEqual(audit["model_latency_ms"], 0)
        self.assertNotIn("reason_redacted", audit)

    async def test_unknown_rule_is_rejected(self):
        async def invoke(**kwargs):
            return assess(hit("other"))
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "rejected")
        self.assertEqual(audit["validation"], "invalid_assessment")

    async def test_invalid_outcome_and_extra_fields_are_rejected(self):
        for item, validation in (({"rule_id": "r", "outcome": "maybe"}, "invalid_assessment"),
                                 ({"rule_id": "r", "outcome": "no_hit", "severity": "low"}, "invalid_assessment_fields"),
                                 ({"rule_id": "r", "outcome": "no_hit", "reason": " "}, "invalid_reason"),
                                 ({"rule_id": "r", "outcome": "review", "action": "mute"}, "invalid_assessment_fields")):
            with self.subTest(item=item):
                async def invoke(**kwargs):
                    return assess(item)
                item_row = self.queue.claim("p", "g")
                result = await self.decider(invoke)(item_row)
                self.assertEqual(result.status, "rejected")
                self.assertEqual(json.loads(result.detail)["validation"], validation)
                self.queue.enqueue(platform_id="p", group_id="g", message_id=f"again-{validation}-{item['outcome']}",
                                   capacity=10, payload={"text": "private message", "sender_id": "u"})
                self.queue.finish(item_row, status=result.status, detail=result.detail)

    async def test_invalid_duration_keeps_the_rejected_value_and_limit(self):
        self.rules.save(**{**self.rule, "expected_version": 1, "duration_mode": "autonomous",
                           "severity_seconds": {}})
        async def invoke(**kwargs):
            self.assertEqual(kwargs["rules"][0]["duration_limit_seconds"], 300)
            return assess(hit(severity=None, duration_seconds="600", reason="spam" * 200))
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "rejected")
        self.assertEqual(audit["validation"], "invalid_duration")
        self.assertEqual(audit["rejected_call"]["duration_seconds"],
                         {"type": "str", "value": "600", "chars": 3})
        self.assertEqual(audit["rejected_call"]["max_duration_seconds"], 300)
        self.assertEqual(audit["rejected_call"]["rule_id"], "r")
        self.assertEqual(len(audit["rejected_call"]["reason"]), 500)
        self.assertEqual(audit["rejected_call"]["reason_chars"], 800)
        self.assertNotIn("proposal", audit)

    async def test_invalid_duration_is_regenerated_before_it_is_final(self):
        self.rules.save(**{**self.rule, "expected_version": 1, "duration_mode": "autonomous",
                           "severity_seconds": {}})
        answers = [assess(hit(severity=None, duration_seconds="600")),
                   assess(hit(severity=None, duration_seconds=120))]
        calls = []
        async def invoke(**kwargs):
            calls.append(kwargs.get("retry_reason"))
            return answers[len(calls) - 1]
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "proposed")
        self.assertEqual(calls, [None, "invalid_duration"])
        self.assertEqual(audit["proposal"]["duration_seconds"], 120)
        self.assertEqual(audit["generation_attempts"], 2)
        self.assertEqual(audit["generation_reasons"], ["invalid_duration"])

    async def test_regeneration_is_bounded_and_the_last_answer_is_recorded(self):
        self.rules.save(**{**self.rule, "expected_version": 1, "duration_mode": "autonomous",
                           "severity_seconds": {}})
        calls = []
        async def invoke(**kwargs):
            calls.append(kwargs.get("retry_reason"))
            return assess(hit(severity=None, duration_seconds=0))
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "rejected")
        self.assertEqual(calls, [None, "invalid_duration", "invalid_duration"])
        self.assertEqual(audit["validation"], "invalid_duration")
        self.assertEqual(audit["generation_attempts"], 3)
        self.assertEqual(audit["generation_reasons"], ["invalid_duration"] * 3)
        self.assertEqual(audit["rejected_call"]["duration_seconds"], {"type": "int", "value": 0})

    async def test_incomplete_and_malformed_answers_are_regenerated(self):
        incomplete = SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["assess_rules"],
            tools_call_args=[{"assessments": [hit()]}],
            raw_completion={"choices": [{"finish_reason": "length"}]})
        legacy = SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=[],
                                 tools_call_args=[], completion_text="NO_ACTION")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM dry_run_queue")
            db.commit()
        for index, (first, reason) in enumerate(((incomplete, "incomplete_model_output"),
                                                 (legacy, "missing_or_multiple_assessments"))):
            with self.subTest(reason=reason):
                self.queue.enqueue(platform_id="p", group_id="g", message_id=f"retry-{index}",
                                   capacity=10, payload={"text": "private message", "sender_id": "u"})
                answers = [first, assess(hit())]
                calls = []
                async def invoke(**kwargs):
                    calls.append(kwargs.get("retry_reason"))
                    return answers[len(calls) - 1]
                status, audit = await self.run_worker(invoke)
                self.assertEqual(status, "proposed")
                self.assertEqual(calls, [None, reason])
                self.assertEqual(audit["generation_attempts"], 2)
                self.assertEqual(audit["generation_reasons"], [reason])
                with closing(sqlite3.connect(self.path)) as db:
                    db.execute("DELETE FROM dry_run_queue")
                    db.commit()

    async def test_retry_stops_when_the_decision_budget_is_used(self):
        self.rules.save(**{**self.rule, "expected_version": 1, "duration_mode": "autonomous",
                           "severity_seconds": {}})
        calls = []
        async def invoke(**kwargs):
            calls.append(kwargs.get("retry_reason"))
            return assess(hit(severity=None, duration_seconds="600"))
        item = self.queue.claim("p", "g")
        result = await self.decider(invoke, retry_budget_seconds=0.0001)(item)
        detail = json.loads(result.detail)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(calls, [None])
        self.assertEqual(detail["retry_skipped"], "time_budget")
        self.assertNotIn("generation_attempts", detail)

    async def test_invalid_generation_settings_are_rejected_at_construction(self):
        for value in (0, 6, True, 2.0, "3"):
            with self.subTest(attempts=value), self.assertRaises(ValueError):
                self.decider(lambda **kwargs: None, max_generation_attempts=value)
        for value in (0, -1, True, "1"):
            with self.subTest(budget=value), self.assertRaises(ValueError):
                self.decider(lambda **kwargs: None, retry_budget_seconds=value)

    async def test_protocol_diagnostics_and_input_fingerprint_are_audited(self):
        async def invoke(**kwargs):
            return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["wrong_tool"],
                tools_call_args=[{"x": 1}], raw_completion={"finish_reason": "tool_calls"})
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "rejected")
        self.assertEqual(audit["response_status"], "invalid_assessment_tool")
        self.assertEqual(audit["detail_version"], 1)
        self.assertEqual(audit["prompt_version"], 3)
        self.assertRegex(audit["input_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(audit["protocol"]["tool_names"], ["wrong_tool"])
        self.assertEqual(audit["protocol"]["argument_types"], ["dict"])
        self.assertEqual(audit["protocol"]["argument_keys"], ["x"])
        self.assertEqual(audit["protocol"]["finish_reason"], "tool_calls")
        self.assertNotIn("raw_tool_arguments", audit)

    async def test_debug_capture_is_opt_in_and_bounded(self):
        async def invoke(**kwargs):
            return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["wrong_tool"],
                tools_call_args=[{"x": "y" * 5000}], raw_completion={"finish_reason": "tool_calls"},
                completion_text="Z" * 2000)
        status, audit = await self.run_worker(invoke, diagnostic_capture=True)
        self.assertEqual(status, "rejected")
        self.assertIn("y" * 100, audit["raw_tool_arguments"])
        self.assertTrue(audit["raw_tool_arguments"].endswith("...[truncated]"))
        self.assertEqual(len(audit["raw_completion_text"]), 1000)

    async def test_legacy_text_and_single_action_protocols_are_rejected(self):
        responses = (
            SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=[], tools_call_args=[],
                            completion_text="NO_ACTION"),
            SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["propose_action"],
                            tools_call_args=[{"violated": True, "rule_id": "r", "severity": "low", "reason": "spam"}]),
        )
        for response in responses:
            with self.subTest(tools=response.tools_call_name):
                async def invoke(**kwargs):
                    return response
                item = self.queue.claim("p", "g")
                result = await self.decider(invoke)(item)
                self.assertEqual(result.status, "rejected")
                self.assertNotIn("proposal", json.loads(result.detail))
                self.queue.finish(item, status=result.status, detail=result.detail)
                self.queue.enqueue(platform_id="p", group_id="g", message_id=f"x{len(response.tools_call_name)}",
                                   capacity=10, payload={"text": "private message", "sender_id": "u"})

    async def test_forbidden_action_is_rejected_without_mute_fallback(self):
        async def invoke(**kwargs):
            return assess(hit(action="reply", reason="rule violated"))
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "rejected")
        self.assertEqual(audit["validation"], "invalid_parameters")
        self.assertNotIn("proposal", audit)

    async def test_language_is_audited_and_summary_reason_is_preserved(self):
        self.rules.save(**{**self.rule, "expected_version": 1, "text": "禁止发布广告"})
        async def invoke(**kwargs):
            self.assertEqual(kwargs["language"], "zh")
            return assess(hit(reason="发布了推广内容"))
        worker = DryRunWorker(self.queue, self.decider(invoke), concurrency=1, timeout_seconds=2)
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
        worker = DryRunWorker(self.queue, self.decider(invoke), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        events = self.queue.list_events(platform_id="p", group_id="g")
        self.assertEqual([event["status"] for event in events], ["started", "failed"])
        self.assertEqual(json.loads(events[0]["detail"])["prompt_language"], "en")
        detail = json.loads(events[1]["detail"])
        self.assertEqual(detail["prompt_language"], "en")
        self.assertEqual(detail["reason_code"], "decision_error")
        self.assertEqual(detail["error"], {"type": "RuntimeError"})
        self.assertNotIn("private provider error", events[1]["detail"])

    async def test_no_hit_reasons_are_recorded_per_rule(self):
        self.add_rule("r2")
        async def invoke(**kwargs):
            return assess(no_hit("r", reason="No spam."), no_hit("r2", reason="private message"))
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "no_action")
        self.assertEqual(audit["reason"], "r: No spam.; r2: private message")
        self.assertEqual(audit["matched_rules"], [])
        self.assertNotIn("proposals", audit)
        self.assertNotIn("reason_redacted", audit)

    async def test_no_hit_reason_is_recorded_as_returned_and_optional(self):
        for index, (extra, expected) in enumerate((({"reason": "No rule was violated."}, "r: No rule was violated."),
                                                   ({"reason": "PRIVATE\u200b  MESSAGE"}, "r: PRIVATE\u200b  MESSAGE"),
                                                   ({}, None))):
            with self.subTest(extra=extra):
                item = self.queue.claim("p", "g")
                async def invoke(**kwargs):
                    return assess(no_hit(**extra))
                result = await self.decider(invoke)(item)
                detail = json.loads(result.detail)
                self.assertEqual(result.status, "no_action")
                self.assertEqual(detail.get("reason"), expected)
                self.assertNotIn("reason_redacted", detail)
                self.queue.finish(item, status=result.status, detail=result.detail, rule_versions=result.rule_versions)
                self.queue.enqueue(platform_id="p", group_id="g", message_id=f"next-{index}", capacity=10,
                                   payload={"text": "private message", "sender_id": "u"})

    async def test_disabled_no_action_reasons_are_discarded(self):
        item = self.queue.claim("p", "g")
        async def invoke(**kwargs):
            return assess(no_hit(reason="Unrequested reason"))
        result = await self.decider(invoke, no_action_reason_enabled=False)(item)
        detail = json.loads(result.detail)
        self.assertEqual(result.status, "no_action")
        self.assertFalse(detail["no_action_reason_enabled"])
        self.assertNotIn("reason", detail)
        self.assertNotIn("Unrequested reason", result.detail)
        self.assertTrue(self.queue.finish(item, status=result.status, detail=result.detail, rule_versions=result.rule_versions))

    async def test_disabled_no_action_reason_keeps_violation_reason(self):
        item = self.queue.claim("p", "g")
        async def invoke(**kwargs):
            return assess(hit(reason="The rule was violated"))
        result = await self.decider(invoke, no_action_reason_enabled=False)(item)
        self.assertEqual(result.status, "proposed")
        self.assertEqual(json.loads(result.detail)["proposal"]["reason"], "The rule was violated")

    async def test_review_outcome_requires_manual_review(self):
        async def invoke(**kwargs):
            return assess({"rule_id": "r", "outcome": "review", "reason": "unclear"})
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "review")
        self.assertEqual(audit["review_reason"], "uncertain_assessment")
        self.assertEqual(audit["assessments"], [{"rule_id": "r", "violated": None}])
        self.assertNotIn("proposals", audit)

    async def test_single_hit_records_one_proposal_and_defaults(self):
        async def invoke(**kwargs):
            return assess(hit())
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "proposed")
        self.assertEqual(audit["selected_rules"], ["r"])
        self.assertEqual(audit["matched_rules"], ["r"])
        self.assertEqual(audit["proposals"], [audit["proposal"]])
        self.assertEqual(audit["equal_priority_policy"], "execute_all")
        self.assertIs(audit["peer_tie"], False)
        self.assertEqual(audit["selection_reason"], "highest_priority_match")
        self.assertNotIn("strategy", audit)

    async def test_higher_priority_hit_wins_alone(self):
        self.add_rule("r2", priority=10)
        async def invoke(**kwargs):
            return assess(hit("r", reason="reason one"), hit("r2", reason="reason two"))
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "proposed")
        self.assertEqual(audit["matched_rules"], ["r", "r2"])
        self.assertEqual(audit["selected_rules"], ["r2"])
        self.assertEqual([proposal["rule_id"] for proposal in audit["proposals"]], ["r2"])
        self.assertEqual(audit["proposal"]["rule_id"], "r2")
        self.assertEqual(audit["selection_reason"], "highest_priority_match")
        self.assertIs(audit["peer_tie"], False)

    async def test_equal_priority_execute_all_proposes_every_tied_rule(self):
        self.add_rule("r2")
        async def invoke(**kwargs):
            return assess(hit("r", reason="reason one"), hit("r2", reason="reason two"))
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "proposed")
        self.assertEqual(audit["selected_rules"], ["r", "r2"])
        self.assertEqual([proposal["rule_id"] for proposal in audit["proposals"]], ["r", "r2"])
        self.assertEqual([proposal["reason"] for proposal in audit["proposals"]], ["reason one", "reason two"])
        self.assertEqual(audit["proposal"], audit["proposals"][0])
        self.assertEqual(audit["selection_reason"], "equal_priority_match")
        self.assertEqual(audit["equal_priority_policy"], "execute_all")
        self.assertIs(audit["peer_tie"], False)
        self.assertNotIn("review_reason", audit)

    async def test_equal_priority_confirm_flags_the_tie(self):
        self.add_rule("r2")
        async def invoke(**kwargs):
            return assess(hit("r2", reason="reason two"), hit("r", reason="reason one"))
        status, audit = await self.run_worker(invoke, equal_priority_policy="confirm")
        self.assertEqual(status, "proposed")
        self.assertEqual(audit["selected_rules"], ["r2", "r"])
        self.assertEqual(audit["proposal"]["rule_id"], "r2")
        self.assertEqual(audit["proposal"], audit["proposals"][0])
        self.assertEqual(audit["equal_priority_policy"], "confirm")
        self.assertIs(audit["peer_tie"], True)
        self.assertEqual(audit["selection_reason"], "equal_priority_match")

    async def test_confirm_policy_does_not_flag_a_single_winner(self):
        self.add_rule("r2", priority=5)
        async def invoke(**kwargs):
            return assess(hit("r"), hit("r2"))
        status, audit = await self.run_worker(invoke, equal_priority_policy="confirm")
        self.assertEqual(status, "proposed")
        self.assertEqual(audit["selected_rules"], ["r2"])
        self.assertIs(audit["peer_tie"], False)

    async def test_invalid_equal_priority_policy_is_rejected_at_construction(self):
        for policy in ("model", "priority", "", None, True):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.decider(lambda **kwargs: None, equal_priority_policy=policy)
        with self.assertRaises(TypeError):
            LlmDecider(self.rules, lambda **kwargs: None, model="llm", max_duration_seconds=300,
                       queue=self.queue, strategy="priority")

    async def test_rule_language_selects_prompt_and_preserves_summary(self):
        self.rules.save(**{**self.rule, "expected_version": 1, "text": "禁止发布广告"})
        async def invoke(**kwargs):
            self.assertEqual(kwargs["language"], "zh")
            self.assertNotIn("strategy", kwargs)
            return assess(hit(reason="发布了推广内容"))
        worker = DryRunWorker(self.queue, self.decider(invoke), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        events = self.queue.list_events(platform_id="p", group_id="g")
        self.assertEqual([json.loads(event["detail"])["prompt_language"] for event in events], ["zh", "zh"])
        self.assertEqual(json.loads(events[-1]["detail"])["proposal"]["reason"], "发布了推广内容")

    async def test_missing_rule_requires_review(self):
        self.add_rule("r2", priority=10)
        async def invoke(**kwargs):
            return assess(no_hit("r"))
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "review")
        self.assertEqual(audit["selection_reason"], "incomplete_or_duplicate_assessment")
        self.assertEqual(audit["review_reason"], "incomplete_or_duplicate_assessment")

    async def test_duplicate_rule_requires_review(self):
        async def invoke(**kwargs):
            return assess(no_hit("r"), no_hit("r"))
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "review")
        self.assertEqual(audit["selection_reason"], "incomplete_or_duplicate_assessment")

    async def test_uncertain_lower_priority_rule_blocks_the_higher_hit(self):
        self.add_rule("r2", priority=10)
        async def invoke(**kwargs):
            return assess({"rule_id": "r", "outcome": "review"}, hit("r2"))
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "review")
        self.assertEqual(audit["selection_reason"], "uncertain_assessment")
        self.assertNotIn("proposals", audit)

    async def test_does_not_ignore_invalid_lower_priority_hit(self):
        self.add_rule("r2", priority=10)
        async def invoke(**kwargs):
            return assess(hit("r", severity="unknown", reason="reason one"), hit("r2", reason="reason two"))
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "rejected")
        self.assertEqual(audit["validation"], "invalid_severity")
        self.assertNotIn("proposal", audit)
        self.assertNotIn("proposals", audit)

    async def test_invalid_tied_hit_rejects_the_whole_decision(self):
        self.add_rule("r2")
        async def invoke(**kwargs):
            return assess(hit("r"), hit("r2", severity="unknown"))
        status, audit = await self.run_worker(invoke)
        self.assertEqual(status, "rejected")
        self.assertEqual(audit["validation"], "invalid_severity")
        self.assertNotIn("proposals", audit)

    def enqueue_image(self, text=""):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM dry_run_queue")
            db.execute("DELETE FROM dry_run_events")
            db.commit()
        self.queue.enqueue(platform_id="p", group_id="g", message_id="img", capacity=10,
                           payload={"text": text, "sender_id": "u", "image_urls": [IMAGE_URL]})

    def raw_audit(self):
        with closing(sqlite3.connect(self.path)) as db:
            return [tuple(row) for row in db.execute("SELECT status, detail FROM dry_run_events ORDER BY id")]

    def image_decider(self, invoker, *, fetcher=None, prefetcher=None, image_mode="direct",
                      image_enabled=True, image_model="vision"):
        return LlmDecider(self.rules, invoker, model="llm", max_duration_seconds=300, queue=self.queue,
                          image_enabled=image_enabled, image_model=image_model, image_mode=image_mode,
                          caption_prompt="describe the image", fetcher=fetcher or Fetcher(),
                          prefetcher=prefetcher or Prefetcher())

    async def test_direct_image_mode_uses_the_image_model_without_auditing_the_image(self):
        self.enqueue_image()
        invoker = ImageInvoker()
        worker = DryRunWorker(self.queue, self.image_decider(invoker), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, audit = self.audit()
        self.assertEqual(status, "proposed")
        self.assertEqual(invoker.calls[0]["model"], "vision")
        self.assertEqual(invoker.calls[0]["image_urls"], [IMAGE.data_url])
        self.assertNotIn("image_caption", invoker.calls[0])
        self.assertEqual(audit["image"]["mode"], "direct")
        self.assertEqual(audit["image"]["count"], 1)
        self.assertEqual(audit["image"]["source"], "inline")
        self.assertEqual(audit["message_chars"], 0)
        rendered = json.dumps(audit)
        self.assertNotIn(IMAGE_URL, rendered)
        self.assertNotIn(IMAGE.data_url, rendered)

    async def test_image_preview_is_stored_for_the_record(self):
        self.enqueue_image()
        SettingsStore(self.path, Path(str(self.path) + ".backup")).load_or_migrate({})
        worker = DryRunWorker(self.queue, self.image_decider(
            ImageInvoker(), fetcher=Fetcher(result=(IMAGE_WITH_PREVIEW,))),
            concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        images = self.queue.message_images(platform_id="p", group_id="g", message_id="img")
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].startswith("data:image/jpeg;base64,"))

    async def test_caption_mode_keeps_the_text_model_and_hides_the_caption_from_audit(self):
        self.enqueue_image(text="hello")
        invoker = ImageInvoker(caption="a poster with a code")
        worker = DryRunWorker(self.queue, self.image_decider(invoker, image_mode="caption"),
                              concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, audit = self.audit()
        self.assertEqual(status, "proposed")
        self.assertEqual(invoker.caption_calls[0]["model"], "vision")
        self.assertEqual(invoker.caption_calls[0]["prompt"], "describe the image")
        self.assertEqual(invoker.calls[0]["model"], "llm")
        self.assertNotIn("image_urls", invoker.calls[0])
        self.assertEqual(invoker.calls[0]["image_caption"], "a poster with a code")
        self.assertEqual(audit["image"]["mode"], "caption")
        self.assertEqual(audit["image"]["caption_chars"], len("a poster with a code"))
        self.assertNotIn("a poster with a code", json.dumps(audit))

    async def test_prefetched_images_are_used_without_downloading_again(self):
        self.enqueue_image()
        fetcher = Fetcher()
        prefetcher = Prefetcher(PrefetchedImages((IMAGE,), None, len(IMAGE.data_url)))
        worker = DryRunWorker(self.queue, self.image_decider(ImageInvoker(), fetcher=fetcher,
                                                             prefetcher=prefetcher),
                              concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        status, audit = self.audit()
        self.assertEqual(status, "proposed")
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(prefetcher.keys, [("p", "g", "img")])
        self.assertEqual(audit["image"]["source"], "prefetch")

    async def test_image_failures_skip_without_calling_a_model(self):
        self.enqueue_image()
        invoker = ImageInvoker()
        worker = DryRunWorker(self.queue, self.image_decider(invoker, fetcher=Fetcher(
            error=ImageFetchError("image_too_large"))), concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        self.assertEqual(self.raw_audit(), [("skipped", "image_too_large")])
        self.assertEqual(invoker.calls, [])
        self.assertEqual(invoker.caption_calls, [])

    async def test_caption_failures_skip_without_calling_the_judge(self):
        for caption, error, expected in ((None, RuntimeError("private"), "image_caption_failed"),
                                         (None, None, "image_caption_empty")):
            with self.subTest(expected=expected):
                self.enqueue_image()
                invoker = ImageInvoker(caption=caption, caption_error=error)
                worker = DryRunWorker(self.queue, self.image_decider(invoker, image_mode="caption"),
                                      concurrency=1, timeout_seconds=2)
                await worker.drain_group("p", "g")
                self.assertEqual(self.raw_audit(), [("skipped", expected)])
                self.assertEqual(invoker.calls, [])

    async def test_images_are_skipped_when_the_group_switch_is_off(self):
        self.enqueue_image()
        invoker = ImageInvoker()
        worker = DryRunWorker(self.queue, self.image_decider(invoker, image_enabled=False),
                              concurrency=1, timeout_seconds=2)
        await worker.drain_group("p", "g")
        self.assertEqual(self.raw_audit(), [("skipped", "image_judging_disabled")])
        self.assertEqual(invoker.calls, [])
