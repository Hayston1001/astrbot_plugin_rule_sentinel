import unittest

from sentinel.sentinel_core import RuleSnapshot, validate_proposals


class ProposalValidationTests(unittest.TestCase):
    def setUp(self):
        self.rule = RuleSnapshot("r1", 2, True, frozenset({"mute"}), "strict", {"low": 60})
        self.call = {"action": "mute", "rule_id": "r1", "reason": "reason", "severity": "low"}

    def validate(self, calls, rule=None, limit=300, sender="sender", backend="llm"):
        return validate_proposals(calls, sender_id=sender, rules={"r1": rule or self.rule}, max_duration_seconds=limit, decision_backend=backend)

    def test_strict_proposal_uses_trusted_target_version_and_mapping(self):
        result = self.validate([self.call])
        self.assertEqual(result.status, "proposed")
        self.assertEqual((result.proposal.target_sender_id, result.proposal.rule_version, result.proposal.duration_seconds), ("sender", 2, 60))

    def test_no_action_and_conflicting_calls(self):
        self.assertEqual(self.validate([]).status, "no_action")
        self.assertEqual(self.validate([self.call, self.call]).reason, "multiple_tool_calls")
        self.assertEqual(self.validate(None).status, "rejected")

    def test_model_cannot_choose_target_or_version(self):
        for field in ("target_sender_id", "rule_version"):
            with self.subTest(field=field):
                self.assertEqual(self.validate([{**self.call, field: "other"}]).reason, "invalid_parameters")

    def test_disabled_unknown_and_unsupported(self):
        disabled = RuleSnapshot("r1", 2, False, frozenset({"mute"}), "strict", {"low": 60})
        self.assertEqual(self.validate([self.call], rule=disabled).status, "rejected")
        self.assertEqual(self.validate([{**self.call, "rule_id": "unknown"}]).reason, "unknown_rule")
        self.assertEqual(self.validate([{**self.call, "action": "ban"}]).reason, "unsupported_action")

    def test_autonomous_duration_is_bounded_and_integer(self):
        rule = RuleSnapshot("r1", 1, True, frozenset({"mute"}), "autonomous", {})
        call = {"action": "mute", "rule_id": "r1", "reason": "reason"}
        for duration in (0, -1, 301, True, 1.5, "60"):
            with self.subTest(duration=duration):
                self.assertEqual(self.validate([{**call, "duration_seconds": duration}], rule=rule).status, "rejected")
        self.assertEqual(self.validate([{**call, "duration_seconds": 300}], rule=rule).proposal.duration_seconds, 300)

    def test_invalid_mapping_sender_reason_and_limit(self):
        self.assertEqual(self.validate([{**self.call, "severity": "unknown"}]).reason, "invalid_severity")
        self.assertEqual(self.validate([self.call], limit=30).reason, "invalid_duration")
        self.assertEqual(self.validate([self.call], sender="").reason, "invalid_sender")
        self.assertEqual(self.validate([{**self.call, "reason": " "}]).reason, "invalid_reason")
        self.assertEqual(self.validate([self.call], limit=True).reason, "invalid_duration_limit")

    def test_reasons_are_required_and_unsupported_backends_are_rejected(self):
        call = {key: value for key, value in self.call.items() if key != "reason"}
        self.assertEqual(self.validate([call]).reason, "invalid_reason")
        self.assertEqual(self.validate([], backend="unknown").reason, "invalid_decision_backend")
        self.assertEqual(self.validate([self.call], backend="jev").reason, "invalid_decision_backend")

    def test_actions_must_be_allowed_and_reply_text_is_trusted(self):
        action = {"type": "reply", "mode": "confirm", "template": "{sender_id} / {rule_id}"}
        rule = RuleSnapshot("r1", 2, True, frozenset({"reply"}), "strict", {}, [action])
        call = {"action": "reply", "rule_id": "r1", "reason": "rule violated"}
        result = self.validate([call], rule=rule)
        self.assertEqual(result.status, "proposed")
        self.assertEqual((result.proposal.target_sender_id, result.proposal.rule_version), ("sender", 2))
        self.assertEqual((result.proposal.mode, result.proposal.template), ("confirm", action["template"]))
        for field in ("template", "text", "target_sender_id", "rule_version"):
            self.assertEqual(self.validate([{**call, field: "attacker"}], rule=rule).reason, "invalid_parameters")
        self.assertEqual(self.validate([{**call, "action": "recall"}], rule=rule).reason, "rule_not_enabled_for_action")
        self.assertEqual(self.validate([{**call, "action": "mute"}], rule=rule).reason, "rule_not_enabled_for_action")


if __name__ == "__main__":
    unittest.main()
