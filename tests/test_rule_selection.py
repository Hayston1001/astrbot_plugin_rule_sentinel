import unittest

from sentinel.rule_selection import RuleAssessment, select_rules


class RuleSelectionTests(unittest.TestCase):
    def test_highest_priority_match_wins_alone(self):
        result = select_rules(enabled_priorities={"a": 10, "b": 5, "c": 1},
            assessments=[RuleAssessment("a", False), RuleAssessment("b", True), RuleAssessment("c", True)])
        self.assertEqual((result.status, result.rule_ids, result.reason, result.peer_tie),
                         ("selected", ("b",), "highest_priority_match", False))

    def test_single_winner_ignores_equal_priority_policy(self):
        for policy in ("execute_all", "confirm"):
            with self.subTest(policy=policy):
                result = select_rules(enabled_priorities={"a": 2, "b": 1},
                    assessments=[RuleAssessment("a", True), RuleAssessment("b", True)],
                    equal_priority_policy=policy)
                self.assertEqual((result.rule_ids, result.peer_tie), (("a",), False))

    def test_tie_returns_every_tied_rule_and_flags_only_confirm(self):
        priorities = {"a": 1, "b": 1, "c": 0}
        assessments = [RuleAssessment("a", True), RuleAssessment("b", True), RuleAssessment("c", True)]
        default = select_rules(enabled_priorities=priorities, assessments=assessments)
        explicit = select_rules(enabled_priorities=priorities, assessments=assessments,
                                equal_priority_policy="execute_all")
        confirm = select_rules(enabled_priorities=priorities, assessments=assessments,
                               equal_priority_policy="confirm")
        for result in (default, explicit):
            self.assertEqual((result.status, result.rule_ids, result.reason, result.peer_tie),
                             ("selected", ("a", "b"), "equal_priority_match", False))
        self.assertEqual((confirm.status, confirm.rule_ids, confirm.reason, confirm.peer_tie),
                         ("selected", ("a", "b"), "equal_priority_match", True))

    def test_tied_rules_keep_assessment_order(self):
        result = select_rules(enabled_priorities={"a": 1, "b": 1},
            assessments=[RuleAssessment("b", True), RuleAssessment("a", True)])
        self.assertEqual(result.rule_ids, ("b", "a"))

    def test_lower_priority_tie_does_not_count(self):
        result = select_rules(enabled_priorities={"a": 5, "b": 1, "c": 1},
            assessments=[RuleAssessment("a", True), RuleAssessment("b", True), RuleAssessment("c", True)],
            equal_priority_policy="confirm")
        self.assertEqual((result.rule_ids, result.peer_tie), (("a",), False))

    def test_unknown_assessment_requires_review(self):
        result = select_rules(enabled_priorities={"a": 1, "b": 1},
            assessments=[RuleAssessment("a", True), RuleAssessment("b", None)])
        self.assertEqual((result.status, result.reason, result.rule_ids), ("review", "uncertain_assessment", ()))

    def test_missing_and_duplicate_assessments_require_review(self):
        priorities = {"a": 1, "b": 2}
        for assessments in ([RuleAssessment("a", False)],
                            [RuleAssessment("a", False), RuleAssessment("a", False)],
                            [RuleAssessment("a", False), RuleAssessment("b", False), RuleAssessment("c", False)],
                            [RuleAssessment("a", False), RuleAssessment("x", False)]):
            with self.subTest(count=len(assessments)):
                result = select_rules(enabled_priorities=priorities, assessments=assessments)
                self.assertEqual((result.status, result.reason), ("review", "incomplete_or_duplicate_assessment"))

    def test_blank_rule_identity_requires_review(self):
        result = select_rules(enabled_priorities={"a": 1},
                              assessments=[RuleAssessment(" ", False)])
        self.assertEqual((result.status, result.reason), ("review", "invalid_rule_identity"))

    def test_no_match_is_no_action(self):
        result = select_rules(enabled_priorities={"a": 1, "b": 2},
            assessments=[RuleAssessment("a", False), RuleAssessment("b", False)])
        self.assertEqual((result.status, result.rule_ids, result.reason, result.peer_tie),
                         ("no_action", (), "no_matching_rules", False))

    def test_invalid_policy_and_inputs_raise(self):
        assessments = [RuleAssessment("a", True)]
        for policy in ("model", "priority", "", None, True):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                select_rules(enabled_priorities={"a": 1}, assessments=assessments, equal_priority_policy=policy)
        for priorities in ({}, {"a": True}, {"a": "1"}, {"": 1}):
            with self.subTest(priorities=priorities), self.assertRaises(ValueError):
                select_rules(enabled_priorities=priorities, assessments=assessments)
        for bad in (tuple(assessments), ["a"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                select_rules(enabled_priorities={"a": 1}, assessments=bad)
