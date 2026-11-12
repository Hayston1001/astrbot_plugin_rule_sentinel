import unittest

from sentinel.prompts import PROMPTS, detect_language, select_prompt_language


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
            for key in ("reason", "system_model", "system_priority"):
                with self.subTest(language=language, key=key):
                    self.assertIn(phrase, PROMPTS[language][key])
