import unittest

from sentinel.jev_protocol import InvalidJevAction, JevBatchChoices, JevChoices, build_group_request, build_priority_request, build_request, parse_batch_response, parse_priority_response, parse_response


class JevProtocolTests(unittest.TestCase):
    def setUp(self):
        self.choices = build_request(model="selected-model", message="hello", rule_text="rule", severity_seconds={"low": 60})
        self.response = {"model": "version", "answers": {"decision": {"type": "choice", "choice": "severity_0", "confidence": 0.8, "probabilities": {"no_action": 0.1, "review": 0.1, "severity_0": 0.8}}}}

    def test_proposal_preserves_evidence_without_reason(self):
        result = parse_response(self.response, self.choices, "rule1")
        self.assertEqual(result["call"], {"violated": True, "rule_id": "rule1", "severity": "low"})
        self.assertEqual(result["model"], "version")
        self.assertEqual(result["probabilities"]["severity_0"], 0.8)

    def test_review_is_not_no_action(self):
        answer = self.response["answers"]["decision"]
        answer.update(choice="review", probabilities={"no_action": 0.1, "review": 0.8, "severity_0": 0.1})
        self.assertEqual(parse_response(self.response, self.choices, "r")["status"], "review")

    def test_bad_results_do_not_turn_into_no_action(self):
        answer = self.response["answers"]["decision"]
        for changes in ({"choice": "unknown"}, {"confidence": True}, {"probabilities": {}},
                        {"probabilities": {"no_action": 0.1, "review": 0.1, "severity_0": float("nan")}}):
            with self.subTest(changes=changes):
                original = dict(answer)
                answer.update(changes)
                with self.assertRaises(ValueError):
                    parse_response(self.response, self.choices, "r")
                answer.clear()
                answer.update(original)

    def test_explicit_mapping_required(self):
        with self.assertRaises(ValueError):
            build_request(model="m", message="x", rule_text="r", severity_seconds={})

    def test_priority_requires_every_rule_answer(self):
        rules = [{"rule_id": "a", "text": "rule a", "allowed_actions": ["mute"], "duration_mode": "strict", "severity_seconds": {"low": 1}},
                 {"rule_id": "b", "text": "rule b", "allowed_actions": ["mute"], "duration_mode": "strict", "severity_seconds": {"low": 2}}]
        choices = build_priority_request(model="m", message="x", rules=rules)
        response = {"model": "m", "answers": {"rule_0": {"type": "choice", "choice": "no_action",
                    "confidence": 0.8, "probabilities": {"no_action": 0.8, "review": 0.1, "severity_0": 0.1}}}}
        with self.assertRaises(ValueError):
            parse_priority_response(response, choices, {"a": 1, "b": 2})

    def test_candidates_are_split_without_losing_options(self):
        mapping = {f"level_{i:03}": i + 1 for i in range(70)}
        rules = [{"rule_id": "r1", "text": "rule", "actions": [{"type": "mute", "mode": "auto", "duration_mode": "strict", "severity_seconds": mapping}]}]
        choices = build_group_request(model="m", message="x", rules=rules)
        self.assertIsInstance(choices, JevBatchChoices)
        self.assertTrue(all(len(question["criteria"]) <= 64 for question in choices.request["questions"].values()))
        self.assertEqual(sum(len(batch.action_by_option) for batch in choices.by_question.values()), 70)
        answers = {}
        selected = None
        for index, (name, batch) in enumerate(choices.by_question.items()):
            options = list(batch.action_by_option)
            chosen = options[0] if index == 0 else "no_action"
            if index == 0:
                selected = chosen
            probabilities = {option: 0.0 for option in ["no_action", "review", *options]}
            probabilities[chosen] = 1.0
            answers[name] = {"type": "choice", "choice": chosen, "confidence": 1.0, "probabilities": probabilities}
        result = parse_batch_response({"model": "m", "answers": answers}, choices)
        self.assertEqual((result["status"], result["choice"]), ("proposed", selected))
        self.assertTrue(result["call"]["violated"])
        self.assertIn("r1_hit_", selected)

    def test_response_action_field_is_not_authoritative(self):
        self.response["answers"]["decision"]["action"] = "recall"
        self.assertNotIn("action", parse_response(self.response, self.choices, "r")["call"])

    def test_missing_action_mapping_is_rejected_not_assumed_mute(self):
        broken = JevChoices(self.choices.request, self.choices.severity_by_option)
        with self.assertRaises(InvalidJevAction):
            parse_response(self.response, broken, "r")
        with self.assertRaises(ValueError):
            build_group_request(model="m", message="x", rules=[{"rule_id": "r", "text": "rule", "severity_seconds": {"low": 1}}])

    def test_priority_batches_preserve_candidates_and_review_conflicts(self):
        mapping = {f"level_{i:03}": i + 1 for i in range(70)}
        rules = [{"rule_id": "r", "text": "rule", "actions": [{"type": "mute", "mode": "auto", "duration_mode": "strict", "severity_seconds": mapping}]}]
        choices = build_priority_request(model="m", message="x", rules=rules)
        self.assertEqual(len(choices.by_question), 2)
        self.assertTrue(all(len(question["criteria"]) <= 64 for question in choices.request["questions"].values()))
        self.assertEqual(sum(len(part.action_by_option) for _, part in choices.by_question.values()), 70)
        answers = {}
        for name, (_, part) in choices.by_question.items():
            options = list(part.action_by_option)
            selected = options[0]
            probabilities = {option: 0.0 for option in ["no_action", "review", *options]}
            probabilities[selected] = 1.0
            answers[name] = {"type": "choice", "choice": selected, "confidence": 1.0, "probabilities": probabilities}
        result = parse_priority_response({"model": "m", "answers": answers}, choices, {"r": 1})
        self.assertEqual(result["status"], "review")
        self.assertEqual(len(result["assessments"]["r"]["choices"]), 2)


if __name__ == "__main__":
    unittest.main()
