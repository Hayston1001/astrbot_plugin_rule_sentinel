import unittest

from sentinel.rule_selection import RuleAssessment, select_rule


class RuleSelectionTests(unittest.TestCase):
    def test_priority_only_considers_matched_rules(self):
        result = select_rule(enabled_priorities={"a": 10, "b": 5},
            assessments=[RuleAssessment("a", False), RuleAssessment("b", True)], strategy="priority")
        self.assertEqual((result.status, result.rule_id), ("selected", "b"))

    def test_equal_priority_and_unknown_require_review(self):
        priorities = {"a": 1, "b": 1}
        result = select_rule(enabled_priorities=priorities,
            assessments=[RuleAssessment("a", True), RuleAssessment("b", True)], strategy="priority")
        self.assertEqual(result.reason, "equal_priority_conflict")
        result = select_rule(enabled_priorities=priorities,
            assessments=[RuleAssessment("a", True), RuleAssessment("b", None)], strategy="priority")
        self.assertEqual(result.reason, "uncertain_assessment")

    def test_model_cannot_choose_unmatched_rule(self):
        result = select_rule(enabled_priorities={"a": 1, "b": 2},
            assessments=[RuleAssessment("a", True), RuleAssessment("b", False)], strategy="model", model_choice="b")
        self.assertEqual(result.reason, "invalid_model_choice")

    def test_missing_duplicate_and_no_match_are_distinct(self):
        priorities = {"a": 1, "b": 2}
        for assessments in ([RuleAssessment("a", False)], [RuleAssessment("a", False), RuleAssessment("a", False)]):
            self.assertEqual(select_rule(enabled_priorities=priorities, assessments=assessments,
                                         strategy="priority").status, "review")
        result = select_rule(enabled_priorities=priorities,
            assessments=[RuleAssessment("a", False), RuleAssessment("b", False)], strategy="priority")
        self.assertEqual(result.status, "no_action")
