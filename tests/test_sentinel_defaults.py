import unittest
import json
from pathlib import Path

from sentinel.monitoring_policy import eligibility
from sentinel.sentinel_defaults import DEFAULT_MONITORING_POLICY, DEFAULT_QUEUE_CAPACITY_PER_GROUP
from sentinel.settings_store import DEFAULTS


class DefaultTests(unittest.TestCase):
    def test_confirmed_safe_defaults(self):
        self.assertEqual(DEFAULT_QUEUE_CAPACITY_PER_GROUP, 100)
        self.assertEqual(DEFAULT_MONITORING_POLICY.mode, "allowlist")
        self.assertEqual(DEFAULT_MONITORING_POLICY.groups, frozenset())
        schema = json.loads((Path(__file__).resolve().parent.parent / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema, {})
        self.assertEqual(DEFAULTS["queue_capacity_per_group"], DEFAULT_QUEUE_CAPACITY_PER_GROUP)
        self.assertNotIn("monitoring_enabled", DEFAULTS)
        self.assertTrue(DEFAULTS["action_execution_enabled"])
        self.assertEqual(DEFAULTS["decision_backend"], "llm")
        self.assertEqual(DEFAULTS["llm_provider_id"], "")
        self.assertEqual(eligibility(DEFAULT_MONITORING_POLICY, platform_id="p", group_id="g",
                                     sender_id="u", bot_id="b", has_enabled_rule=True,
                                     is_management_command=False, is_text=True), "group_not_allowed")


if __name__ == "__main__":
    unittest.main()
