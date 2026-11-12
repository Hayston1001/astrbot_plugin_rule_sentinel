import unittest
from enum import Enum
from types import SimpleNamespace

from sentinel.llm_protocol import normalize_priority_response, normalize_response


class LlmProtocolTests(unittest.TestCase):
    def response(self, names=None, args=None, **extra):
        return SimpleNamespace(role=extra.get("role", "assistant"), is_chunk=extra.get("is_chunk", False),
                               completion_text=extra.get("completion_text", "NO_ACTION"),
                               tools_call_name=names if names is not None else [],
                               tools_call_args=args if args is not None else [])

    def test_tool_proposal_is_only_data(self):
        args = {"violated": True, "rule_id": "r", "severity": "low", "reason": "reason"}
        result = normalize_response(self.response(["propose_action"], [args]))
        self.assertEqual(result.status, "proposed")
        self.assertEqual(result.calls, [args])
        tool_result = normalize_response(self.response(["propose_action"], [args], role="tool"))
        self.assertEqual(tool_result.calls, [args])

    def test_bad_or_incomplete_responses_are_not_no_action(self):
        for response in (self.response(role="err"), self.response(is_chunk=True),
                         self.response(["propose_mute"], []), SimpleNamespace()):
            with self.subTest(response=response):
                self.assertEqual(normalize_response(response).status, "failed")
        self.assertEqual(normalize_response(self.response()).status, "no_action")
        self.assertEqual(normalize_response(self.response(completion_text="")).status, "failed")
        self.assertEqual(normalize_response(self.response(completion_text="probably fine")).status, "failed")
        self.assertEqual(normalize_response(self.response(role="tool")).reason, "empty_tool_response")

    def test_no_action_accepts_an_optional_bounded_reason_only_after_marker(self):
        for text, explanation in (("NO_ACTION", None), ("NO_ACTION\nNo violation.", "No violation."),
                                  (" NO_ACTION\r\n  没有违反规则  ", "没有违反规则")):
            with self.subTest(text=text):
                result = normalize_response(self.response(completion_text=text))
                self.assertEqual(result.status, "no_action")
                self.assertEqual(result.explanation, explanation)
        for text in ("NO_ACTION extra", "Reason\nNO_ACTION", "NO_ACTION\n" + "x" * 2001):
            with self.subTest(text=text[:40]):
                self.assertEqual(normalize_response(self.response(completion_text=text)).status, "failed")

    def test_rejects_multiple_unknown_and_forged_action(self):
        self.assertEqual(normalize_response(self.response(["propose_mute", "propose_mute"], [{}, {}])).status, "rejected")
        self.assertEqual(normalize_response(self.response(["delete"], [{}])).status, "rejected")
        self.assertEqual(normalize_response(self.response(["propose_action"], [{"action": "other"}])).status, "rejected")
        self.assertEqual(normalize_response(self.response(["propose_action"], [{}])).status, "rejected")

    def test_explicit_truncation_is_not_no_action_or_proposal(self):
        for names, args in (([], []), (["propose_mute"], [{"rule_id": "r"}])):
            response = self.response(names, args)
            response.raw_completion = {"choices": [{"finish_reason": "length"}]}
            self.assertEqual(normalize_response(response).reason, "incomplete_model_output")

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
            for names, args in (([], []), (["propose_mute"], [{"rule_id": "r"}])):
                with self.subTest(raw=raw, names=names):
                    response = self.response(names, args)
                    response.raw_completion = raw
                    self.assertEqual(normalize_response(response).reason, "incomplete_model_output")
            response = self.response(["assess_rules"], [{"assessments": []}], role="tool")
            response.raw_completion = raw
            self.assertEqual(normalize_priority_response(response).reason, "incomplete_model_output")
        response = self.response()
        response.raw_completion = SimpleNamespace(candidates=[SimpleNamespace(finish_reason=FinishReason.STOP)])
        self.assertEqual(normalize_response(response).status, "no_action")

    def test_priority_requires_exactly_one_batch_tool(self):
        self.assertEqual(normalize_priority_response(self.response()).status, "rejected")
        response = self.response(["assess_rules", "assess_rules"], [{"assessments": []}, {"assessments": []}])
        self.assertEqual(normalize_priority_response(response).reason, "missing_or_multiple_assessments")
        response = self.response(["assess_rules"], [{"assessments": []}], role="tool")
        self.assertEqual(normalize_priority_response(response).status, "assessments")

    def test_unknown_or_missing_finish_metadata_is_not_success(self):
        for raw in ({"choices": [{"finish_reason": None}]}, {"choices": []},
                    {"choices": [{"finish_reason": "new_unknown_reason"}]},
                    {"candidates": [{"finish_reason": "future_safety_flag"}]},
                    {"status": "future_status"}, {"stop_reason": "refusal"},
                    {"prompt_feedback": {"block_reason": "safety"}}):
            for names, args in (([], []), (["propose_mute"], [{"rule_id": "r"}])):
                response = self.response(names, args)
                response.raw_completion = raw
                with self.subTest(raw=raw, names=names):
                    self.assertEqual(normalize_response(response).reason, "incomplete_model_output")

    def test_openai_finish_reason_matches_the_parsed_tool_calls(self):
        for reason, names, args, expected in (
            ("tool_calls", [], [], "inconsistent_finish_reason"),
            ("function_call", [], [], "inconsistent_finish_reason"),
            ("stop", ["propose_mute"], [{"rule_id": "r"}], "inconsistent_finish_reason"),
            ("tool_calls", ["propose_action"], [{"rule_id": "r", "violated": True}], "proposal_received"),
            ("stop", [], [], "explicit_no_action"),
        ):
            response = self.response(names, args)
            response.raw_completion = {"choices": [{"finish_reason": reason}]}
            with self.subTest(reason=reason, names=names):
                self.assertEqual(normalize_response(response).reason, expected)
        priority = self.response(["assess_rules"], [{"assessments": []}])
        priority.raw_completion = {"choices": [{"finish_reason": "stop"}]}
        self.assertEqual(normalize_priority_response(priority).reason, "inconsistent_finish_reason")
