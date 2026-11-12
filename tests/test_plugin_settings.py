import unittest
from types import SimpleNamespace

from sentinel.plugin_settings import read_settings
from sentinel.policy_guard import PolicyGuard
from sentinel.monitoring_policy import MonitoringPolicy
from sentinel.dry_run_worker import Decision
from sentinel.framework_session_gate import FrameworkSessionGate
import json


class SettingsTests(unittest.TestCase):
    def test_defaults_do_not_choose_model(self):
        settings = read_settings({}, require_model=False)
        self.assertEqual(settings.capacity, 100)
        self.assertEqual(settings.policy.mode, "allowlist")
        self.assertEqual(settings.model, "")
        self.assertEqual((settings.max_duration_seconds, settings.concurrency, settings.timeout_seconds), (2592000, 20, 35))
        self.assertEqual((settings.global_rate_per_minute, settings.group_rate_per_minute), (100, 60))
        self.assertEqual((settings.failure_threshold, settings.cooldown_seconds), (5, 120))

    def test_monitoring_requires_valid_settings(self):
        with self.assertRaises(ValueError):
            read_settings({})
        base = {"decision_backend": "llm", "llm_provider_id": "selected"}
        for changes in ({"queue_capacity_per_group": 0}, {"monitoring_groups": ["bad"]},
                        {"max_duration_seconds": 2592001}, {"monitoring_groups": ["__rule_sentinel_system__/x"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                read_settings({**base, **changes})
        settings = read_settings({**base, "monitoring_groups": ["p/g"]})
        self.assertEqual(settings.policy.groups, frozenset({("p", "g")}))
        self.assertEqual(read_settings({"model_concurrency": 5}, require_model=False).concurrency, 5)


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_mismatched_queued_session_never_reaches_model(self):
        async def allow(*args):
            return True
        async def decide(item):
            self.fail("mismatched session reached model")
        gate = FrameworkSessionGate(get_config=lambda origin: {"platform_settings": {
            "enable_id_white_list": False, "id_whitelist": []}},
            is_session_enabled=allow, is_plugin_enabled=allow, plugin_name="plugin")
        guard = PolicyGuard(decide, lambda: MonitoringPolicy("allowlist", frozenset({("p", "g")})),
                            session_gate=gate)
        result = await guard(SimpleNamespace(platform_id="p", group_id="g",
                                              payload={"session_origin": "p:GroupMessage:other"}))
        self.assertEqual((result.status, result.detail), ("skipped", "framework_session_denied"))

    async def test_disabled_group_never_reaches_model(self):
        async def decide(item):
            self.fail("disabled group reached model")
        guard = PolicyGuard(decide, lambda: MonitoringPolicy("allowlist", frozenset()))
        result = await guard(SimpleNamespace(platform_id="p", group_id="g"))
        self.assertEqual((result.status, result.detail), ("skipped", "group_not_allowed"))

    async def test_session_revocation_prevents_queued_model_call(self):
        async def decide(item):
            self.fail("revoked session reached model")
        async def denied(session):
            self.assertEqual(session.session_origin, "p:GroupMessage:g")
            return False
        guard = PolicyGuard(decide, lambda: MonitoringPolicy("allowlist", frozenset({("p", "g")})),
                            session_gate=denied)
        result = await guard(SimpleNamespace(platform_id="p", group_id="g", payload={"session_origin": "p:GroupMessage:g"}))
        self.assertEqual((result.status, result.detail), ("skipped", "framework_session_denied"))

    async def test_session_revoked_during_model_call_marks_result_for_review(self):
        allowed = [True]
        async def gate(session):
            return allowed[0]
        async def decide(item):
            allowed[0] = False
            return Decision("proposed", json.dumps({"proposal": {"rule_id": "r"}}))
        guard = PolicyGuard(decide, lambda: MonitoringPolicy("allowlist", frozenset({("p", "g")})),
                            session_gate=gate)
        result = await guard(SimpleNamespace(platform_id="p", group_id="g", payload={"session_origin": "p:GroupMessage:g"}))
        self.assertEqual(result.status, "review")
        self.assertEqual(json.loads(result.detail)["review_reason"], "framework_session_denied")
