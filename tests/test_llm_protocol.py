import unittest
from enum import Enum
from types import SimpleNamespace

from sentinel.llm_protocol import normalize_assessment_response


class LlmProtocolTests(unittest.TestCase):
    def response(self, names=None, args=None, **extra):
        return SimpleNamespace(role=extra.get("role", "assistant"), is_chunk=extra.get("is_chunk", False),
                               completion_text=extra.get("completion_text", ""),
                               tools_call_name=names if names is not None else [],
                               tools_call_args=args if args is not None else [])

    def assessment(self, **extra):
        args = [{"assessments": [{"rule_id": "r", "outcome": "no_hit"}]}]
        return self.response(["assess_rules"], args, **extra)

    def test_assessment_is_only_data(self):
        items = [{"rule_id": "r", "outcome": "hit", "severity": "low", "reason": "reason"},
                 {"rule_id": "s", "outcome": "no_hit"}]
        for role in ("assistant", "tool"):
            with self.subTest(role=role):
                result = normalize_assessment_response(
                    self.response(["assess_rules"], [{"assessments": items}], role=role))
                self.assertEqual((result.status, result.reason), ("assessments", "assessment_received"))
                self.assertEqual(result.calls, items)

    def test_bad_or_incomplete_responses_fail(self):
        for response in (self.response(role="err"), self.response(is_chunk=True), SimpleNamespace()):
            with self.subTest(response=response):
                self.assertEqual(normalize_assessment_response(response).status, "failed")
        malformed = self.response(["assess_rules"], [])
        self.assertEqual(normalize_assessment_response(malformed).reason, "malformed_tool_calls")

    def test_text_only_answers_are_never_accepted(self):
        for text in ("NO_ACTION", "NEEDS_REVIEW", "probably fine", ""):
            with self.subTest(text=text):
                result = normalize_assessment_response(self.response(completion_text=text))
                self.assertEqual((result.status, result.reason), ("rejected", "missing_or_multiple_assessments"))

    def test_rejects_wrong_multiple_and_forged_tools(self):
        for names, args in ((["propose_action"], [{"violated": True, "rule_id": "r"}]),
                            (["propose_mute"], [{}]), (["delete"], [{}]),
                            (["assess_rules"], [{}]),
                            (["assess_rules"], [{"assessments": [], "extra": 1}]),
                            (["assess_rules"], [["not", "a", "dict"]])):
            with self.subTest(names=names, args=args):
                result = normalize_assessment_response(self.response(names, args))
                self.assertEqual((result.status, result.reason), ("rejected", "invalid_assessment_tool"))
        multiple = self.response(["assess_rules", "assess_rules"], [{"assessments": []}, {"assessments": []}])
        self.assertEqual(normalize_assessment_response(multiple).reason, "missing_or_multiple_assessments")

    def test_assessment_list_shape_is_checked(self):
        for value in ("text", {"rule_id": "r"}, None, ["r"], [1]):
            with self.subTest(value=value):
                result = normalize_assessment_response(self.response(["assess_rules"], [{"assessments": value}]))
                self.assertEqual((result.status, result.reason), ("rejected", "invalid_assessments"))

    def test_explicit_truncation_is_not_an_assessment(self):
        for response in (self.response(), self.assessment()):
            response.raw_completion = {"choices": [{"finish_reason": "length"}]}
            self.assertEqual(normalize_assessment_response(response).reason, "incomplete_model_output")

    def test_provider_specific_incomplete_reasons_are_not_accepted(self):
        class FinishReason(Enum):
            MAX_TOKENS = 2
            STOP = 1
        raw_results = [
            {"candidates": [{"finish_reason": "MAX_TOKENS"}]},
            SimpleNamespace(candidates=[SimpleNamespace(finish_reason=FinishReason.MAX_TOKENS)]),
            {"candidates": [{"finish_reason": "FinishReason.SAFETY"}]},
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
            {"status": "in_progress"},
            {"stop_reason": "max_tokens"},
        ]
        for raw in raw_results:
            with self.subTest(raw=raw):
                response = self.assessment(role="tool")
                response.raw_completion = raw
                self.assertEqual(normalize_assessment_response(response).reason, "incomplete_model_output")
        response = self.assessment()
        response.raw_completion = {"candidates": [{"finish_reason": "stop"}]}
        self.assertEqual(normalize_assessment_response(response).status, "assessments")

    def test_unknown_or_missing_finish_metadata_is_not_success(self):
        for raw in ({"choices": [{"finish_reason": None}]}, {"choices": []},
                    {"choices": [{"finish_reason": "new_unknown_reason"}]},
                    {"candidates": [{"finish_reason": "future_safety_flag"}]},
                    {"status": "future_status"}, {"stop_reason": "refusal"},
                    {"prompt_feedback": {"block_reason": "safety"}}):
            response = self.assessment()
            response.raw_completion = raw
            with self.subTest(raw=raw):
                self.assertEqual(normalize_assessment_response(response).reason, "incomplete_model_output")

    def test_openai_finish_reason_matches_the_parsed_tool_calls(self):
        for reason, names, expected in (
            ("tool_calls", [], "inconsistent_finish_reason"),
            ("function_call", [], "inconsistent_finish_reason"),
            ("stop", ["assess_rules"], "inconsistent_finish_reason"),
            ("tool_calls", ["assess_rules"], "assessment_received"),
            ("stop", [], "missing_or_multiple_assessments"),
        ):
            response = self.response(names, [{"assessments": []}] * len(names))
            response.raw_completion = {"choices": [{"finish_reason": reason}]}
            with self.subTest(reason=reason, names=names):
                self.assertEqual(normalize_assessment_response(response).reason, expected)

    def test_failure_diagnostics_keep_shapes_not_values(self):
        response = SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["wrong_tool"],
            tools_call_args=[{"secret": "value"}], raw_completion={"finish_reason": "tool_calls"})
        result = normalize_assessment_response(response)
        self.assertEqual(result.reason, "invalid_assessment_tool")
        self.assertEqual(result.diagnostics["tool_names"], ["wrong_tool"])
        self.assertEqual(result.diagnostics["argument_types"], ["dict"])
        self.assertEqual(result.diagnostics["argument_keys"], ["secret"])
        self.assertEqual(result.diagnostics["finish_reason"], "tool_calls")
        self.assertNotIn("value", str(result.diagnostics))
