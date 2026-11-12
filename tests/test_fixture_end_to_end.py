"""Synthetic pipeline regression, not a measurement of model accuracy."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from sentinel.dry_run_ingest import DryRunAdmission, IncomingMessage
from sentinel.dry_run_queue import DryRunQueue
from sentinel.dry_run_worker import DryRunWorker
from sentinel.evaluation import compare_group_audits, summarize_group_audit
from sentinel.framework_session_gate import FrameworkSessionGate
from sentinel.jev_decider import JevDecider
from sentinel.llm_decider import LlmDecider
from sentinel.monitoring_policy import MonitoringPolicy
from sentinel.rule_store import RuleStore


class FixturePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_fixture_through_admission_decision_and_evaluation(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "moderation_samples.json").read_text(encoding="utf-8"))
        # Scripted outcomes are independent of labels and deliberately include
        # a false positive, false negative, timeout and rejected tool call.
        outcomes = {"positive-01": "proposed", "positive-02": "no_action", "positive-03": "proposed",
                    "negative-01": "no_action", "negative-02": "proposed", "negative-03": "no_action",
                    "negative-04": "timed_out", "adversarial-01": "proposed", "adversarial-02": "no_action",
                    "adversarial-03": "rejected", "adversarial-04": "no_action"}
        self.assertEqual(set(outcomes), {sample["id"] for sample in fixture["samples"]})
        scripted = {sample["message"]: outcomes[sample["id"]] for sample in fixture["samples"]}
        self.assertEqual(len(scripted), len(fixture["samples"]))

        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "state.sqlite"
            rules, queue = RuleStore(database), DryRunQueue(database)
            rules.initialize()
            queue.initialize()
            rules.save(platform_id="qq", group_id="g", rule_id="r", expected_version=0,
                       text=fixture["rule"], enabled=True, allowed_actions=["mute"], duration_mode="strict",
                       severity_seconds={"low": 60}, modified_by="admin")
            async def allow(*args):
                return True
            gate = FrameworkSessionGate(get_config=lambda origin: {"platform_settings": {
                "enable_id_white_list": True, "id_whitelist": ["g"]}},
                is_session_enabled=allow, is_plugin_enabled=allow, plugin_name="plugin")
            policy = MonitoringPolicy("allowlist", frozenset({("qq", "g")}))
            admission = DryRunAdmission(queue, rules, policy=policy, capacity=100, model="synthetic", session_gate=gate)
            for sample in fixture["samples"]:
                message = IncomingMessage("qq", "g", sample["id"], "sender", None, sample["message"],
                                          True, False, sender_is_verified_non_bot=True,
                                          session_origin="qq:GroupMessage:g")
                self.assertEqual(await admission.submit(message), "queued")

            async def invoke(*, model, message, rules, strategy, language):
                self.assertEqual(model, "synthetic")
                self.assertEqual(rules[0]["text"], fixture["rule"])
                outcome = scripted[message]
                if outcome == "timed_out":
                    raise TimeoutError("synthetic timeout")
                if outcome == "no_action":
                    return SimpleNamespace(role="assistant", is_chunk=False, completion_text="NO_ACTION",
                                           tools_call_name=[], tools_call_args=[])
                name = "unknown_tool" if outcome == "rejected" else "propose_action"
                return SimpleNamespace(role="tool", is_chunk=False, tools_call_name=[name],
                    tools_call_args=[{"violated": True, "rule_id": "r", "severity": "low", "reason": "synthetic rule match"}])

            worker = DryRunWorker(queue, LlmDecider(rules, invoke, model="synthetic", max_duration_seconds=300,
                                                   queue=queue), concurrency=1, timeout_seconds=2)
            self.assertEqual(await worker.drain_group("qq", "g"), len(fixture["samples"]))
            events = queue.list_events(platform_id="qq", group_id="g", limit=100)
            labels = [{"message_id": sample["id"], "violation": sample["violation"]} for sample in fixture["samples"]]
            report = summarize_group_audit(labels, events)
            self.assertEqual((report["true_positive"], report["false_negative"], report["false_positive"],
                              report["true_negative"], report["unprocessed"]), (3, 1, 1, 4, 2))
            self.assertEqual((report["unprocessed_positive"], report["unprocessed_negative"]), (1, 1))
            self.assertEqual(report["status_counts"]["timed_out"], 1)
            self.assertEqual(report["status_counts"]["rejected"], 1)
            self.assertEqual(report["status_counts"]["failed"], 0)
            self.assertEqual(report["missing_audit"], 0)
            self.assertEqual(report["false_positive_rate"], 1 / 5)
            self.assertEqual(report["false_negative_rate"], 1 / 4)
            for event in events:
                for sample in fixture["samples"]:
                    self.assertNotIn(sample["message"], event["detail"])
                if event["status"] == "proposed":
                    self.assertEqual(json.loads(event["detail"])["proposal"]["target_sender_id"], "sender")
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM dry_run_queue WHERE payload IS NOT NULL").fetchone()[0], 0)

            jev_outcomes = {"positive-01": "r_hit_low_0", "positive-02": "r_hit_low_0", "positive-03": "r_hit_low_0",
                            "negative-01": "no_action", "negative-02": "no_action", "negative-03": "no_action",
                            "negative-04": "no_action", "adversarial-01": "review", "adversarial-02": "no_action",
                            "adversarial-03": "r_hit_low_0", "adversarial-04": "no_action"}
            self.assertEqual(set(jev_outcomes), set(outcomes))
            scripted_jev = {sample["message"]: jev_outcomes[sample["id"]] for sample in fixture["samples"]}
            jev_database = Path(temp) / "jev.sqlite"
            jev_rules, jev_queue = RuleStore(jev_database), DryRunQueue(jev_database)
            jev_rules.initialize()
            jev_queue.initialize()
            jev_rules.save(platform_id="qq", group_id="g", rule_id="r", expected_version=0,
                           text=fixture["rule"], enabled=True, allowed_actions=["mute"], duration_mode="strict",
                           severity_seconds={"low": 60}, modified_by="admin")
            jev_admission = DryRunAdmission(jev_queue, jev_rules, policy=policy, capacity=100,
                                            model="synthetic-jev", session_gate=gate)
            for sample in fixture["samples"]:
                message = IncomingMessage("qq", "g", sample["id"], "sender", None, sample["message"],
                                          True, False, sender_is_verified_non_bot=True,
                                          session_origin="qq:GroupMessage:g")
                self.assertEqual(await jev_admission.submit(message), "queued")
            async def send(request):
                self.assertEqual(request["model"], "synthetic-jev")
                self.assertEqual(request["state"]["rules"][0]["text"], fixture["rule"])
                choice = scripted_jev[request["state"]["message_untrusted"]]
                probabilities = {option: 0.8 if option == choice else 0.1
                                 for option in ("r_hit_low_0", "no_action", "review")}
                return {"model": "synthetic-jev", "answers": {"decision": {"type": "choice", "choice": choice,
                    "confidence": 0.8, "probabilities": probabilities}}}
            jev_worker = DryRunWorker(jev_queue, JevDecider(jev_rules, send, model="synthetic-jev",
                max_duration_seconds=300, queue=jev_queue), concurrency=1, timeout_seconds=2)
            self.assertEqual(await jev_worker.drain_group("qq", "g"), len(fixture["samples"]))
            jev_events = jev_queue.list_events(platform_id="qq", group_id="g", limit=100)
            comparison = compare_group_audits(labels, events, jev_events)
            self.assertEqual((comparison["paired_completed"], comparison["not_paired"],
                              comparison["paired_disagreements"]), (8, 3, 2))
            self.assertEqual((comparison["first_paired"]["true_positive"], comparison["first_paired"]["false_negative"],
                              comparison["first_paired"]["false_positive"]), (2, 1, 1))
            self.assertEqual((comparison["second_paired"]["true_positive"], comparison["second_paired"]["false_negative"],
                              comparison["second_paired"]["false_positive"]), (3, 0, 0))
            self.assertEqual(comparison["second_coverage"]["status_counts"]["review"], 1)
            for event in jev_events:
                for sample in fixture["samples"]:
                    self.assertNotIn(sample["message"], event["detail"])
                if event["status"] == "proposed":
                    detail = json.loads(event["detail"])
                    self.assertEqual(detail["proposal"]["target_sender_id"], "sender")
                    self.assertIsNone(detail["reason"])
            with closing(sqlite3.connect(jev_database)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM dry_run_queue WHERE payload IS NOT NULL").fetchone()[0], 0)
