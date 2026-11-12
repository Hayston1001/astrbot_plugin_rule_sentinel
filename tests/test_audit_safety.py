import unittest

from sentinel.audit_safety import normalize_for_echo, safe_resolved_model


class AuditSafetyTests(unittest.TestCase):
    def test_model_identifier_does_not_echo_message(self):
        self.assertIsNone(safe_resolved_model("secret-message", "secret-message"))
        self.assertIsNone(safe_resolved_model("this contains user text", "user text"))
        self.assertEqual(safe_resolved_model("model-1.13", "unrelated text"), "model-1.13")

    def test_normalization_detects_whitespace_and_invisible_echoes(self):
        self.assertEqual(normalize_for_echo("PRIVATE\u200b  MESSAGE"), normalize_for_echo("private message"))
