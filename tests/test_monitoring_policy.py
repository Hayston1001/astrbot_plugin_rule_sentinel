import unittest

from sentinel.monitoring_policy import MonitoringPolicy, eligibility


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.message = dict(platform_id="p", group_id="g", sender_id="u", bot_id="bot",
                            has_enabled_rule=True, is_management_command=False, is_text=True)
        self.policy = MonitoringPolicy("allowlist", frozenset({("p", "g")}))

    def test_eligible_and_not_allowed(self):
        self.assertEqual(eligibility(self.policy, **self.message), "eligible")
        self.assertEqual(eligibility(MonitoringPolicy("allowlist", frozenset()), **self.message), "group_not_allowed")

    def test_lists_are_scoped_to_platform(self):
        self.assertEqual(eligibility(self.policy, **{**self.message, "platform_id": "other"}), "group_not_allowed")
        self.assertEqual(eligibility(MonitoringPolicy("blocklist", frozenset({("p", "g")})), **self.message), "group_blocked")
        self.assertEqual(eligibility(MonitoringPolicy("blocklist", frozenset()), **self.message), "eligible")

    def test_default_exclusions_and_missing_identity(self):
        for changes, result in (({"sender_id": "bot"}, "self_message"),
                                ({"sender_id": ""}, "invalid_message_identity"),
                                ({"is_management_command": True}, "management_command_or_unknown"),
                                ({"is_text": False}, "non_text_or_unknown"),
                                ({"has_enabled_rule": False}, "no_enabled_rule")):
            with self.subTest(changes=changes):
                self.assertEqual(eligibility(self.policy, **{**self.message, **changes}), result)

    def test_images_need_the_per_group_switch(self):
        image = {"is_text": False, "has_images": True}
        self.assertEqual(eligibility(self.policy, **{**self.message, **image}), "image_judging_disabled")
        self.assertEqual(eligibility(self.policy, **{**self.message, **image, "image_enabled": True}), "eligible")
        self.assertEqual(eligibility(self.policy, **{**self.message, **image, "image_enabled": True,
                                                     "has_enabled_rule": False}), "no_enabled_rule")
        self.assertEqual(eligibility(self.policy, **{**self.message, "is_text": False,
                                                     "has_images": False, "image_enabled": True}),
                         "non_text_or_unknown")


if __name__ == "__main__":
    unittest.main()
