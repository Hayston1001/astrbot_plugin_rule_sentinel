import unittest

from sentinel.action_config import (
    REPLY_DEFAULTS, intent_mode_allowed, legacy_rule_mode, migrate_legacy_actions, resolve_action_mode,
    resolve_final_mode, resolve_reply_options, rule_mode_for_fields, validate_actions, validate_equal_priority_policy,
    validate_execution_mode,
)


class ActionConfigTests(unittest.TestCase):
    def test_legacy_mute_and_disabled_action(self):
        self.assertEqual(migrate_legacy_actions(["mute"], "strict", {"low": 30}),
                         [{"type": "mute", "duration_mode": "strict", "severity_seconds": {"low": 30}}])
        self.assertEqual(migrate_legacy_actions([], "strict", {"low": 30}), [])

    def test_valid_actions_are_copied(self):
        actions = [{"type": "reply", "template": "{sender_id}: {rule_id}"},
                   {"type": "mute", "duration_mode": "autonomous", "severity_seconds": {}},
                   {"type": "recall"}]
        self.assertEqual(validate_actions(actions), actions)
        self.assertIsNot(validate_actions(actions)[1]["severity_seconds"], actions[1]["severity_seconds"])

    def test_reply_options_inherit_independently_without_rewriting_old_actions(self):
        action = {"type": "reply", "template": "notice"}
        self.assertEqual(validate_actions([action]), [action])
        self.assertEqual(resolve_reply_options(action, REPLY_DEFAULTS), {"quote": True, "mention_sender": False})
        override = {**action, "quote": False}
        self.assertEqual(validate_actions([override]), [override])
        self.assertEqual(resolve_reply_options(override, {**REPLY_DEFAULTS, "reply_mention_sender": True}),
                         {"quote": False, "mention_sender": True})

    def test_reject_invalid_actions(self):
        bad = [[{"type": "kick"}],
               [{"type": "recall"}, {"type": "recall"}],
               [{"type": "recall", "mode": "other"}],
               [{"type": "reply"}],
               [{"type": "reply", "template": "{sender_id.__class__}"}],
               [{"type": "reply", "template": "{rule_id!r}"}],
               [{"type": "reply", "template": "ok", "quote": 1}],
               [{"type": "reply", "template": "ok", "mention_sender": "false"}],
               [{"type": "recall", "quote": True}],
               [{"type": "mute", "duration_mode": "strict", "severity_seconds": {}}],
               [{"type": "mute", "duration_mode": "autonomous", "severity_seconds": {"low": 10}}]]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_actions(value)

    def test_legacy_per_action_modes_are_validated_and_dropped(self):
        legacy = [{"type": "reply", "mode": "confirm", "template": "notice"},
                  {"type": "recall", "mode": "inherit"}]
        self.assertEqual(validate_actions(legacy), [{"type": "reply", "template": "notice"}, {"type": "recall"}])
        self.assertEqual(legacy[0]["mode"], "confirm")
        for mode in ("other", None, 1):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_actions([{"type": "recall", "mode": mode}])

    def test_legacy_rule_mode_branches(self):
        self.assertEqual(legacy_rule_mode([{"type": "reply", "mode": "auto"}, {"type": "recall", "mode": "confirm"}]), "confirm")
        self.assertEqual(legacy_rule_mode([{"type": "reply", "mode": "inherit"}, {"type": "recall", "mode": "auto"}]), "auto")
        self.assertEqual(legacy_rule_mode([{"type": "reply", "mode": "inherit"}, {"type": "recall", "mode": "inherit"}]), "inherit")
        self.assertEqual(legacy_rule_mode([{"type": "recall"}]), "inherit")
        with self.assertRaises(ValueError):
            legacy_rule_mode([{"type": "recall", "mode": "other"}])

    def test_rule_mode_for_fields_prefers_explicit_value(self):
        legacy = [{"type": "recall", "mode": "confirm"}]
        self.assertEqual(rule_mode_for_fields("auto", legacy), "auto")
        self.assertEqual(rule_mode_for_fields(None, legacy), "confirm")
        self.assertEqual(rule_mode_for_fields(None, None), "auto")
        with self.assertRaises(ValueError):
            rule_mode_for_fields("other", legacy)
        with self.assertRaises(ValueError):
            rule_mode_for_fields(None, "recall")

    def test_execution_mode_and_equal_priority_validation(self):
        for mode in ("auto", "confirm", "inherit"):
            self.assertEqual(validate_execution_mode(mode), mode)
        for value in ("", "AUTO", None, 1, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_execution_mode(value)
                with self.assertRaises(ValueError):
                    validate_equal_priority_policy(value)
        for policy in ("execute_all", "confirm"):
            self.assertEqual(validate_equal_priority_policy(policy), policy)

    def test_final_mode_gives_one_reason(self):
        cases = [("auto", False, ("auto", None)), ("auto", True, ("confirm", "peer_tie")),
                 ("confirm", True, ("confirm", "rule")), ("confirm", False, ("confirm", "rule"))]
        for rule_mode, tie, expected in cases:
            with self.subTest(rule_mode=rule_mode, tie=tie):
                self.assertEqual(resolve_final_mode(rule_mode, "auto", peer_tie=tie), expected)
        self.assertEqual(resolve_final_mode("inherit", "confirm", peer_tie=True), ("confirm", "rule"))
        self.assertEqual(resolve_final_mode("inherit", "auto", peer_tie=True), ("confirm", "peer_tie"))
        for default in (None, "invalid"):
            with self.assertRaises(ValueError):
                resolve_final_mode("inherit", default)
        self.assertEqual(resolve_final_mode("confirm", None), ("confirm", "rule"))
        self.assertEqual(resolve_action_mode("inherit", "confirm"), "confirm")

    def test_intent_mode_allowed_accepts_stricter_manual_only(self):
        self.assertTrue(intent_mode_allowed("auto", "auto"))
        self.assertTrue(intent_mode_allowed("auto", "confirm"))
        self.assertTrue(intent_mode_allowed("confirm", "confirm"))
        self.assertFalse(intent_mode_allowed("confirm", "auto"))
        self.assertTrue(intent_mode_allowed("inherit", "auto", "auto"))
        self.assertFalse(intent_mode_allowed("inherit", "auto", "confirm"))
        self.assertTrue(intent_mode_allowed("inherit", "confirm", "confirm"))
        self.assertFalse(intent_mode_allowed("inherit", "auto", None))
        self.assertFalse(intent_mode_allowed("other", "confirm", "auto"))
        self.assertFalse(intent_mode_allowed("auto", "other"))
