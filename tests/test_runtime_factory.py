import tempfile
import unittest
from pathlib import Path

from sentinel.dry_run_queue import DryRunQueue
from sentinel.rule_store import RuleStore
from sentinel.runtime_factory import build_runtime


class FactoryTests(unittest.TestCase):
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

    def test_llm_priority_mode_is_selected_explicitly(self):
        config = {"decision_backend": "llm", "llm_provider_id": "p",
                  "rule_selection_strategy": "priority"}
        runtime = build_runtime(context=object(), config=config, rules=self.rules, queue=self.queue)
        self.assertEqual(runtime.worker.decide.decide.current().strategy, "priority")

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
