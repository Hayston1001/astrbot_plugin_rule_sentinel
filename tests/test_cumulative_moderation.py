import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import closing
from tempfile import TemporaryDirectory
import unittest

from sentinel.dry_run_queue import DryRunQueue, WorkItem
from sentinel.management_service import ManagementService
from sentinel.mute_execution_store import MuteExecutionStore
from sentinel.mute_executor import MuteExecutor
from sentinel.qq_mute_platform import QQMutePlatform
from sentinel.rule_store import RuleStore
from sentinel.sentinel_core import RuleSnapshot, validate_plan_proposal
from sentinel.settings_store import DEFAULTS, SettingsStore
from test_qq_mute_platform import FakeSession


class CumulativeModerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "sentinel.sqlite3"
        self.queue, self.rules = DryRunQueue(path), RuleStore(path)
        self.rules.initialize()
        self.queue.initialize()
        self.settings = SettingsStore(path, path.with_suffix(".json"))
        self.config = self.settings.load_or_migrate({})
        self.config.update(monitoring_groups=["qq/g"], action_execution_enabled=True,
                           decision_backend="llm", llm_provider_id="test-model")
        self.settings.save(self.config)
        self.management = ManagementService(self.rules, self.queue)
        self.session = FakeSession()
        self.executor = MuteExecutor(MuteExecutionStore(path), QQMutePlatform(self.session, lambda _: ("app", "secret")))

    def mute_requests(self):
        return [(method, kwargs.get("json")) for method, url, kwargs in self.session.calls
                if url.endswith("/restrict_chat_setting")]

    def rule(self, rule_id, mode="auto", expected_version=0, text="example"):
        return self.rules.save(platform_id="qq", group_id="g", rule_id=rule_id,
                               expected_version=expected_version, text=text, enabled=True, modified_by="admin",
                               actions=[{"type": "mute", "mode": mode, "duration_mode": "cumulative",
                                         "window_seconds": 60, "tiers_seconds": [10, 30, 45]}])

    def enqueue(self, message):
        self.assertEqual(self.queue.enqueue(platform_id="qq", group_id="g", message_id=message,
                                            payload={"sender_id": "member"}, capacity=10), "queued")

    def decide(self, message, rule, *, enqueue=True):
        if enqueue:
            self.enqueue(message)
        item = self.queue.claim("qq", "g")
        self.assertEqual(item.message_id, message)
        rules = tuple((item.rule_id, item.version) for item in self.rules.current_for_group("qq", "g"))
        self.assertTrue(self.queue.finish(item, status="proposed", rule_versions=rules,
                                         detail=json.dumps({"proposal": {"rule_id": rule.rule_id,
                                             "rule_version": rule.version, "target_sender_id": "member",
                                             "duration_seconds": None}})))
        return self.records()[message]

    def records(self):
        return {row["message_id"]: row for row in self.queue.list_messages(platform_id="qq", group_id="g")}

    async def test_record_only_switch_and_group_override_count_without_execution(self):
        rule = self.rule("rule", "confirm")
        self.config["action_execution_enabled"] = False
        self.settings.save(self.config)
        first = self.decide("first", rule)
        self.assertEqual(first["violation_count"]["ordinal"], 1)
        self.assertEqual(first["execution"]["status"], "inactive")
        self.config["action_execution_enabled"] = True
        self.config["group_overrides"] = {"qq/g": {"execution_mode": "record"}}
        self.settings.save(self.config)
        second = self.decide("second", rule)
        self.assertEqual(second["violation_count"]["ordinal"], 2)
        self.assertEqual(second["execution"]["duration_seconds"], 30)
        self.assertEqual(second["execution"]["history"], [])
        self.assertEqual(self.session.calls, [])

    async def test_confirm_in_actual_order_and_send_calculated_platform_mute(self):
        rule = self.rule("rule", "confirm")
        earlier = self.decide("earlier", rule)
        later = self.decide("later", rule)
        self.assertNotIn("violation_count", earlier)
        self.assertIsNone(earlier["execution"]["duration_seconds"])
        self.assertEqual(self.queue.pending_confirmations("qq", "g")["total_actions"], 2)
        self.assertEqual(await self.executor.execute(later["execution"]["id"], mode="confirm", required_action="mute"), "succeeded")
        # QQ now reports the first mute; append exactly the second tier's duration.
        self.session.query_data = {"members": [{"member_openid": "member",
                                                "mute_expire_at": self.records()["later"]["execution"]["expires_at"]}]}
        self.assertEqual(await self.executor.execute(earlier["execution"]["id"], mode="confirm", required_action="mute"), "succeeded")
        self.assertEqual(await self.executor.execute(later["execution"]["id"], mode="confirm", required_action="mute"), "not_claimed")
        rows = self.records()
        self.assertEqual(rows["later"]["violation_count"]["ordinal"], 1)
        self.assertEqual(rows["earlier"]["violation_count"]["ordinal"], 2)
        self.assertEqual([method for method, _ in self.mute_requests()], ["GET", "POST", "GET", "POST"])
        self.assertEqual(self.mute_requests()[1][1], {"members": [{"op": "add", "member_openid": "member",
                         "mute_expire_at": rows["later"]["execution"]["expires_at"]}]})
        extension = datetime.fromisoformat(rows["later"]["execution"]["expires_at"]) + timedelta(seconds=30)
        self.assertEqual(self.mute_requests()[3][1], {"members": [{"op": "update", "member_openid": "member",
                         "mute_expire_at": extension.isoformat(timespec="milliseconds")}]})
        self.assertEqual(rows["earlier"]["execution"]["history"][-1]["expires_at"],
                         extension.isoformat(timespec="milliseconds"))

    async def test_strict_and_autonomous_both_extend_the_same_way(self):
        for mode, mapping in (("strict", {"low": 60}), ("autonomous", {})):
            with self.subTest(mode=mode):
                rule = self.rules.save(platform_id="qq", group_id="g", rule_id=mode, expected_version=0,
                                       text=mode, enabled=True, modified_by="admin", actions=[
                                           {"type": "mute", "mode": "auto", "duration_mode": mode,
                                            "severity_seconds": mapping}
                                       ])
                self.enqueue(mode)
                item = self.queue.claim("qq", "g")
                revisions = tuple((row.rule_id, row.version) for row in self.rules.current_for_group("qq", "g"))
                self.queue.finish(item, status="proposed", rule_versions=revisions, detail=json.dumps({
                    "proposal": {"rule_id": rule.rule_id, "rule_version": rule.version,
                                 "target_sender_id": "member", "duration_seconds": 60}}))
                previous = (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(microsecond=0)
                self.session.query_data = {"members": [{"member_openid": "member",
                                                        "mute_expire_at": previous.isoformat()}]}
                row = self.records()[mode]
                self.assertEqual(await self.executor.execute(row["execution"]["id"], mode="auto"), "succeeded")
                applied = self.mute_requests()[-1][1]["members"][0]
                self.assertEqual(applied["op"], "update")
                self.assertEqual(datetime.fromisoformat(applied["mute_expire_at"]), previous + timedelta(seconds=60))
                self.assertNotIn("violation_count", self.records()[mode])

    async def test_failed_extension_does_not_retry_or_remove_violation(self):
        rule = self.rule("rule")
        previous = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.session.query_data = {"members": [{"member_openid": "member", "mute_expire_at": previous.isoformat()}]}
        self.session.mute_status = 400
        row = self.decide("first", rule)
        self.assertEqual(await self.executor.execute(row["execution"]["id"], mode="auto"), "failed")
        self.assertEqual([method for method, _ in self.mute_requests()], ["GET", "POST"])
        self.assertEqual(self.records()["first"]["violation_count"]["ordinal"], 1)

    async def test_shared_list_excludes_unselected_rules_and_honors_revoke(self):
        a, b, c = [self.rule(key) for key in "abc"]
        self.config["group_overrides"] = {"qq/g": {"shared_violation_rules": ["a", "b"]}}
        self.settings.save(self.config)
        first = self.decide("one", a)
        self.assertEqual(first["violation_count"]["ordinal"], 1)
        self.assertEqual(self.queue.pending_auto_intent(WorkItem("qq", "g", "one", {}, first["attempt_id"])),
                         first["execution"]["id"])
        self.assertEqual(self.decide("two", b)["violation_count"]["ordinal"], 2)
        self.assertEqual(self.decide("three", c)["violation_count"]["ordinal"], 1)
        params = {"platform_id": "qq", "group_id": "g", "message_id": "one"}
        with self.assertRaises(PermissionError):
            self.management.revoke_count("api_key:bad", params)
        revoked = self.management.revoke_count("admin", params)
        self.assertEqual(self.management.revoke_count("other", params), revoked)
        self.assertEqual(revoked["revoked_by"], "admin")
        self.assertEqual(self.records()["one"]["execution"], first["execution"])
        self.assertEqual(self.decide("four", b)["violation_count"]["ordinal"], 2)

    async def test_multiple_shared_groups_keep_independent_counts_and_allow_regrouping(self):
        a, b, c, d, e = [self.rule(key) for key in "abcde"]
        self.config["group_overrides"] = {"qq/g": {"shared_violation_groups": [
            {"name": "推广", "rule_ids": ["a", "b"]}, {"name": "刷屏", "rule_ids": ["c", "d"]},
        ]}}
        self.settings.save(self.config)
        for index, (rule, expected) in enumerate([(a, 1), (c, 1), (b, 2), (d, 2), (e, 1)], 1):
            self.assertEqual(self.decide(str(index), rule)["violation_count"]["ordinal"], expected)
        self.config["group_overrides"]["qq/g"]["shared_violation_groups"] = [
            {"name": "调整后", "rule_ids": ["a", "c"]},
        ]
        self.settings.save(self.config)
        self.assertEqual(self.decide("six", a)["violation_count"]["ordinal"], 3)
        self.assertEqual(self.decide("seven", b)["violation_count"]["ordinal"], 2)

    async def test_automatic_new_mute_uses_ledger_tier_and_unknown_is_not_retried(self):
        rule = self.rule("rule")
        first = self.decide("first", rule)
        self.assertEqual(await self.executor.execute(first["execution"]["id"], mode="auto"), "succeeded")
        second = self.decide("second", rule)
        self.session.mute_status = 500
        self.assertEqual(await self.executor.execute(second["execution"]["id"], mode="auto"), "unknown")
        self.assertEqual(second["execution"]["duration_seconds"], 30)
        expiry = datetime.fromisoformat(second["execution"]["expires_at"])
        self.assertGreater(expiry, datetime.fromisoformat(first["execution"]["expires_at"]))
        self.assertEqual(self.records()["second"]["violation_count"]["ordinal"], 2)
        third = self.decide("third", rule)
        self.assertEqual(await self.executor.execute(third["execution"]["id"], mode="auto"), "review")
        self.assertEqual(self.records()["third"]["execution"]["reason_code"], "earlier_mute_unresolved")
        self.assertEqual([method for method, _ in self.mute_requests()], ["GET", "POST", "GET", "POST"])

    async def test_invalid_status_response_does_not_write_and_does_not_undo_count(self):
        rule = self.rule("rule")
        for index, (status, payload) in enumerate([(429, {}), (200, {}), (200, {"members": [], "has_more": True})], 1):
            self.session.query_status, self.session.query_data = status, payload
            row = self.decide(str(index), rule)
            self.assertEqual(await self.executor.execute(row["execution"]["id"], mode="auto"), "review")
            self.assertEqual(self.records()[str(index)]["violation_count"]["ordinal"], index)
            self.assertEqual(self.records()[str(index)]["execution"]["reason_code"], "mute_status_unavailable")
        self.assertEqual([method for method, _ in self.mute_requests()], ["GET", "GET", "GET"])

    async def test_revoked_pending_automatic_count_never_calls_qq(self):
        rule = self.rule("rule")
        row = self.decide("one", rule)
        self.management.revoke_count("admin", {"platform_id": "qq", "group_id": "g", "message_id": "one"})
        self.assertEqual(await self.executor.execute(row["execution"]["id"], mode="auto"), "not_claimed")
        self.assertEqual(self.session.calls, [])

    async def test_ignores_pre_activation_message_but_keeps_counts_on_edits(self):
        self.enqueue("old")
        rule = self.rule("rule")
        self.assertNotIn("violation_count", self.decide("old", rule, enqueue=False))
        self.assertEqual(self.decide("first", rule)["violation_count"]["ordinal"], 1)
        updated = self.rule("rule", expected_version=1, text="edited")
        self.assertEqual(self.decide("second", updated)["violation_count"]["ordinal"], 2)

    async def test_rule_edit_invalidates_pending_confirmation(self):
        rule = self.rule("rule", "confirm")
        row = self.decide("pending", rule)
        self.rule("rule", "confirm", expected_version=1, text="edited")
        self.assertEqual(await self.executor.execute(row["execution"]["id"], mode="confirm"), "not_claimed")
        self.assertNotIn("violation_count", self.records()["pending"])

    async def test_upgrade_does_not_activate_old_count_only_actions(self):
        rule = self.rule("rule", "confirm")
        old = self.decide("old", rule)
        new = self.decide("new", rule)
        with closing(self.queue._connect()) as db, db:
            db.execute("UPDATE execution_intents SET reason_code='cumulative_on_confirmation' WHERE id=?",
                       (old["execution"]["id"],))
        self.queue.initialize()
        self.assertEqual(self.records()["old"]["execution"]["status"], "inactive")
        self.assertEqual(await self.executor.execute(old["execution"]["id"], mode="confirm"), "not_claimed")
        self.assertEqual(await self.executor.execute(new["execution"]["id"], mode="confirm"), "succeeded")
        self.assertNotIn("violation_count", self.records()["old"])

    async def test_model_cannot_supply_timing_or_severity(self):
        rule = self.rule("rule")
        snapshot = RuleSnapshot(rule.rule_id, rule.version, rule.enabled,
                                frozenset(rule.allowed_actions), rule.duration_mode, rule.severity_seconds, rule.actions)
        verdict = {"rule_id": "rule", "violated": True, "reason": "rule violated"}
        def validate(fields):
            return validate_plan_proposal(fields, sender_id="member", rules={"rule": snapshot},
                                          max_duration_seconds=DEFAULTS["max_duration_seconds"])
        self.assertIsNone(validate(verdict).proposal.duration_seconds)
        for extra in ({"duration_seconds": 10}, {"severity": "low"}):
            self.assertEqual(validate(verdict | extra).status, "rejected")
