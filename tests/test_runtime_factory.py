import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sentinel.dry_run_queue import DryRunQueue
from sentinel.dry_run_worker import Decision
from sentinel.rule_store import RuleStore
from sentinel.runtime_factory import GroupDecider, build_runtime


class FactoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "state.sqlite"
        self.rules, self.queue = RuleStore(path), DryRunQueue(path)
        self.rules.initialize()
        self.queue.initialize()

    def test_default_requires_a_model(self):
        with self.assertRaises(ValueError):
            build_runtime(context=object(), config={}, rules=self.rules, queue=self.queue)

    def test_llm_runtime_requires_selected_model(self):
        config = {"decision_backend": "llm", "llm_provider_id": "p"}
        runtime = build_runtime(context=object(), config=config, rules=self.rules, queue=self.queue)
        self.assertEqual(runtime.admission.capacity, 100)
        self.assertTrue(runtime.admission.settings_match())
        config["queue_capacity_per_group"] = 5
        self.assertFalse(runtime.admission.settings_match())

    def test_retired_backend_is_rejected(self):
        config = {"decision_backend": "jev", "jev_model": "m"}
        with self.assertRaises(ValueError):
            build_runtime(context=object(), config=config, rules=self.rules, queue=self.queue)

    def test_equal_priority_policy_defaults_to_execute_all(self):
        config = {"decision_backend": "llm", "llm_provider_id": "p"}
        runtime = build_runtime(context=object(), config=config, rules=self.rules, queue=self.queue)
        decider = runtime.worker.decide.decide.current()
        self.assertEqual(decider.equal_priority_policy, "execute_all")
        self.assertFalse(hasattr(decider, "strategy"))

    def test_equal_priority_policy_is_passed_to_the_decider(self):
        config = {"decision_backend": "llm", "llm_provider_id": "p", "equal_priority_policy": "confirm"}
        runtime = build_runtime(context=object(), config=config, rules=self.rules, queue=self.queue)
        self.assertEqual(runtime.worker.decide.decide.current().equal_priority_policy, "confirm")

    def test_invalid_equal_priority_policy_is_rejected(self):
        for value in ("model", "priority", "", None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_runtime(context=object(), rules=self.rules, queue=self.queue,
                              config={"decision_backend": "llm", "llm_provider_id": "p",
                                      "equal_priority_policy": value})

    def test_changing_equal_priority_policy_invalidates_the_running_runtime(self):
        config = {"decision_backend": "llm", "llm_provider_id": "p"}
        runtime = build_runtime(context=object(), config=config, rules=self.rules, queue=self.queue)
        self.assertTrue(runtime.admission.settings_match())
        config["equal_priority_policy"] = "confirm"
        self.assertFalse(runtime.admission.settings_match())

    async def test_group_route_has_no_strategy_and_uses_the_global_policy(self):
        config = {"decision_backend": "llm", "llm_provider_id": "p", "equal_priority_policy": "confirm",
                  "group_overrides": {"p/g": {"model": {"backend": "llm", "model": "other"}}}}
        routes = []
        def make(*route):
            routes.append(route)
            async def decide(item):
                return Decision("skipped", "synthetic")
            return decide
        router = GroupDecider(config, make)
        await router(SimpleNamespace(platform_id="p", group_id="g"))
        self.assertEqual(routes, [("llm", "other", "", False, "", "direct", False)])
    def test_group_decider_builds_a_decider_with_the_global_policy(self):
        config = {"decision_backend": "llm", "llm_provider_id": "p", "equal_priority_policy": "confirm",
                  "group_overrides": {"p/g": {"model": {"backend": "llm", "model": "other"}}}}
        runtime = build_runtime(context=object(), config=config, rules=self.rules, queue=self.queue)
        router = runtime.worker.decide.decide
        built = router.make_decider("llm", "other", "", False, "", "direct", False).current()
        self.assertEqual((built.model, built.equal_priority_policy), ("other", "confirm"))
    def test_selected_provider_is_the_only_model_the_invoker_uses(self):
        config = {"decision_backend": "llm", "llm_provider_id": "p"}
        runtime = build_runtime(context=object(), config=config, rules=self.rules, queue=self.queue)
        self.assertEqual(runtime.worker.decide.decide.current().model, "p")
        self.assertFalse(hasattr(runtime.worker.decide.decide.current().invoke, "verified_provider_ids"))

    def test_direct_llm_runtime_reads_reason_switch_without_reloading(self):
        config = {"decision_backend": "llm", "llm_provider_id": "p"}
        runtime = build_runtime(context=object(), config=config, rules=self.rules, queue=self.queue)
        router = runtime.worker.decide.decide
        enabled = router.current()
        self.assertTrue(enabled.no_action_reason_enabled)
        config["llm_no_action_reason_enabled"] = False
        self.assertTrue(runtime.admission.settings_match())
        self.assertFalse(router.current().no_action_reason_enabled)
        config["llm_no_action_reason_enabled"] = True
        self.assertIs(router.current(), enabled)
