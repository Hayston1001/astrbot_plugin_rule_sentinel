import json
import unittest

from sentinel.astrbot_llm import PRIORITY_SYSTEM_PROMPT, PRIORITY_TOOL_PARAMETERS, SYSTEM_PROMPT, TOOL_PARAMETERS
from sentinel.dry_run_ingest import MAX_MESSAGE_CHARS
from sentinel.openai_request_builder import build_openai_request
from sentinel.openai_request_guard import MAX_REQUEST_BYTES, RequestBodyGuard, RequestIntegrityError


class OpenAIRequestBuilderTests(unittest.TestCase):
    def setUp(self):
        self.rules = [{"rule_id": "r", "text": "no spam", "allowed_actions": ["mute"],
                       "duration_mode": "strict", "severity_seconds": {"low": 60}}]

    def build(self, **kwargs):
        return build_openai_request(**{"model": "selected", "message": "private message",
                                       "rules": self.rules, "strategy": "model", **kwargs})

    def test_request_contains_complete_snapshot_and_data_only_tool(self):
        for strategy, name, system, parameters in (("model", "propose_action", SYSTEM_PROMPT, TOOL_PARAMETERS),
                                                     ("priority", "assess_rules", PRIORITY_SYSTEM_PROMPT, PRIORITY_TOOL_PARAMETERS)):
            with self.subTest(strategy=strategy):
                request = self.build(strategy=strategy)
                self.assertEqual(request["messages"][0]["content"], system)
                self.assertEqual(json.loads(request["messages"][1]["content"]),
                                 {"rules": self.rules, "message_untrusted": "private message"})
                tool = request["tools"][0]["function"]
                self.assertEqual((tool["name"], tool["parameters"]), (name, parameters))
                self.assertEqual(request["tool_choice"], "auto")
                self.assertIs(request["stream"], False)
                RequestBodyGuard(request).check(json.dumps(request).encode())

    def test_request_does_not_keep_mutable_rule_or_tool_references(self):
        request = self.build()
        self.rules[0]["text"] = "changed"
        request["tools"][0]["function"]["parameters"]["required"].clear()
        self.assertEqual(json.loads(request["messages"][1]["content"])["rules"][0]["text"], "no spam")
        self.assertEqual(TOOL_PARAMETERS["required"], ["rule_id", "violated", "reason"])

    def test_invalid_inputs_fail_without_including_message(self):
        invalid = [{"model": ""}, {"message": " "}, {"message": "x" * (MAX_MESSAGE_CHARS + 1)},
                   {"rules": []}, {"strategy": "unknown"}, {"rules": self.rules * 2},
                   {"rules": [{**self.rules[0], "extra": "value"}]},
                   {"rules": [{**self.rules[0], "severity_seconds": {"low": True}}]},
                   {"rules": [{**self.rules[0], "allowed_actions": ["delete"]}]},
                   {"rules": [{**self.rules[0], "duration_mode": "autonomous"}]}]
        for kwargs in invalid:
            with self.subTest(kwargs=list(kwargs)), self.assertRaises(RequestIntegrityError) as raised:
                self.build(**kwargs)
            self.assertNotIn("private message", str(raised.exception))

    def test_autonomous_rule_and_unicode_are_preserved(self):
        rules = [{**self.rules[0], "text": "规则内容", "duration_mode": "autonomous", "severity_seconds": {}}]
        request = self.build(message="群消息", rules=rules)
        self.assertEqual(json.loads(request["messages"][1]["content"]),
                         {"rules": rules, "message_untrusted": "群消息"})

    def test_oversized_snapshot_is_rejected_without_truncation(self):
        rules = [{**self.rules[0], "text": "x" * MAX_REQUEST_BYTES}]
        with self.assertRaises(RequestIntegrityError) as raised:
            self.build(rules=rules)
        self.assertNotIn("private message", str(raised.exception))
