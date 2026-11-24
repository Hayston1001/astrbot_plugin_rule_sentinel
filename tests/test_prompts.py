import unittest

from sentinel.prompts import PROMPT_VERSION, PROMPTS, detect_language, select_prompt_language


class PromptTests(unittest.TestCase):
    def test_detection_counts_cjk_and_latin(self):
        for text, expected in (("禁止发布广告", "zh"), ("No advertising", "en"),
                               ("禁止发广告 spam", "zh"), ("禁止 advertisement", "en"),
                               ("中a", None), ("", None), ("123 !", None),
                               ("広告禁止", "zh"), ("광고금지", "zh")):
            with self.subTest(text=text):
                self.assertEqual(detect_language(text), expected)

    def test_rule_language_precedes_message_and_unknown_falls_back(self):
        self.assertEqual(select_prompt_language(["禁止发布广告"], "advertisement"), "zh")
        self.assertEqual(select_prompt_language(["No spam"], "这是一条中文广告"), "en")
        self.assertEqual(select_prompt_language(["123"], "广告"), "zh")
        self.assertEqual(select_prompt_language(["中a"], "hello"), "en")
        self.assertEqual(select_prompt_language(["123"], "!?"), "en")

    def test_locales_have_matching_keys_and_nonquotation_requirements(self):
        self.assertEqual(set(PROMPTS["en"]), set(PROMPTS["zh"]))
        for language, phrase in (("en", "Do not repeat or quote the original message"),
                                 ("zh", "不要复述或引用消息原文")):
            for key in ("reason", "reason_hit", "system", "system_no_reason"):
                with self.subTest(language=language, key=key):
                    self.assertIn(phrase, PROMPTS[language][key])

    def test_prompt_version_and_single_protocol_keys(self):
        self.assertEqual(PROMPT_VERSION, 2)
        for language in ("en", "zh"):
            keys = set(PROMPTS[language])
            self.assertTrue({"system", "system_no_reason", "tool", "assessments", "outcome"} <= keys)
            for removed in ("system_priority", "system_priority_no_reason", "system_model",
                            "system_model_no_reason", "tool_priority", "tool_model", "action",
                            "chat_context_review"):
                self.assertNotIn(removed, keys)

    def test_system_prompts_require_one_assessment_call_per_message(self):
        for key in ("system", "system_no_reason"):
            self.assertIn("assess_rules", PROMPTS["en"][key])
            self.assertIn("exactly once", PROMPTS["en"][key])
            self.assertNotIn("NO_ACTION", PROMPTS["en"][key])
            self.assertIn("assess_rules", PROMPTS["zh"][key])
            self.assertNotIn("NO_ACTION", PROMPTS["zh"][key])
