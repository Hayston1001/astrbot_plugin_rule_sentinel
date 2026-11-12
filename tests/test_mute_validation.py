import unittest
from types import SimpleNamespace

from sentinel.mute_validation import revalidate_mute


class MuteValidationTests(unittest.TestCase):
    def setUp(self):
        self.rule = SimpleNamespace(platform_id="p", group_id="g", rule_id="r", version=2, enabled=True,
            actions=[{"type": "mute", "mode": "confirm", "duration_mode": "strict", "severity_seconds": {"low": 60}}])
        self.proposal = {"platform_id": "p", "group_id": "g", "action": "mute", "rule_id": "r", "rule_version": 2, "target_sender_id": "u",
                         "duration_seconds": 60, "mode": "confirm"}

    def check(self, proposal=None, **kwargs):
        context = {"rule": self.rule, "max_duration_seconds": 300}
        context.update(kwargs)
        return revalidate_mute(self.proposal if proposal is None else proposal, **context)

    def test_valid_and_forged_inputs(self):
        self.assertTrue(self.check().allowed)
        for field, value, reason in (("target_sender_id", "", "invalid_message_identity"),
                                     ("group_id", "other", "rule_scope_mismatch"),
                                     ("rule_version", 1, "rule_version_changed"),
                                     ("mode", "auto", "action_not_allowed"),
                                     ("duration_seconds", 61, "duration_not_in_rule"),
                                     ("duration_seconds", True, "invalid_duration"),
                                     ("action", "recall", "unsupported_action")):
            with self.subTest(field=field):
                self.assertEqual(self.check({**self.proposal, field: value}).reason, reason)
        self.assertEqual(self.check(max_duration_seconds=30).reason, "invalid_duration")
        self.rule.enabled = False
        self.assertEqual(self.check().reason, "rule_disabled")

    def test_autonomous_duration_is_bounded(self):
        self.rule.actions[0].update(duration_mode="autonomous", severity_seconds={})
        self.assertTrue(self.check({**self.proposal, "duration_seconds": 300}).allowed)
        self.assertEqual(self.check({**self.proposal, "duration_seconds": 301}).reason, "invalid_duration")
