import unittest

from sentinel.sentinel_core import RuleSnapshot, validate_plan_proposal


class PlanProposalTests(unittest.TestCase):
    def setUp(self):
        actions = ({"type": "recall", "mode": "auto"},
                   {"type": "reply", "mode": "auto", "template": "notice"},
                   {"type": "mute", "mode": "auto", "duration_mode": "strict", "severity_seconds": {"low": 60}})
        self.rule = RuleSnapshot("r", 3, True, frozenset({"recall", "reply", "mute"}), "strict", {"low": 60}, actions)

    def validate(self, call):
        return validate_plan_proposal({"violated": True, **call}, sender_id="sender", rules={"r": self.rule}, max_duration_seconds=300)

    def test_duration_and_identity_come_from_trusted_context(self):
        result = self.validate({"rule_id": "r", "severity": "low", "reason": "violation"})
        self.assertEqual(result.status, "proposed")
        self.assertEqual((result.proposal.rule_version, result.proposal.target_sender_id, result.proposal.duration_seconds),
                         (3, "sender", 60))

    def test_model_cannot_supply_action_target_or_version(self):
        valid = {"rule_id": "r", "severity": "low", "reason": "violation"}
        for key, value in (("action", "mute"), ("target_sender_id", "other"), ("rule_version", 3)):
            with self.subTest(key=key):
                self.assertEqual(self.validate({**valid, key: value}).status, "rejected")

    def test_unmapped_or_excessive_duration_rejected(self):
        self.assertEqual(self.validate({"rule_id": "r", "severity": "high", "reason": "violation"}).reason,
                         "invalid_severity")
        self.rule = RuleSnapshot("r", 3, True, frozenset({"mute"}), "strict", {},
                                 ({"type": "mute", "mode": "auto", "duration_mode": "strict", "severity_seconds": {"low": 301}},))
        self.assertEqual(self.validate({"rule_id": "r", "severity": "low", "reason": "violation"}).reason,
                         "invalid_duration")

    def test_removed_backend_cannot_submit_a_plan(self):
        result = validate_plan_proposal({"violated": True, "rule_id": "r", "severity": "low"},
                                        sender_id="sender", rules={"r": self.rule}, max_duration_seconds=300,
                                        decision_backend="jev")
        self.assertEqual(result.reason, "invalid_decision_backend")


if __name__ == "__main__":
    unittest.main()
