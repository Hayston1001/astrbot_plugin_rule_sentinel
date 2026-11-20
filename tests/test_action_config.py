import unittest

from sentinel.action_config import REPLY_DEFAULTS, migrate_legacy_actions, resolve_reply_options, validate_actions


class ActionConfigTests(unittest.TestCase):
    def test_legacy_mute_and_disabled_action(self):
        self.assertEqual(migrate_legacy_actions(["mute"], "strict", {"low": 30}),
                         [{"type": "mute", "mode": "auto", "duration_mode": "strict", "severity_seconds": {"low": 30}}])
        self.assertEqual(migrate_legacy_actions([], "strict", {"low": 30}), [])

    def test_valid_actions_are_copied(self):
        actions = [{"type": "reply", "mode": "confirm", "template": "{sender_id}: {rule_id}"},
                   {"type": "mute", "mode": "auto", "duration_mode": "autonomous", "severity_seconds": {}},
                   {"type": "recall", "mode": "auto"}]
        self.assertEqual(validate_actions(actions), actions)
        self.assertIsNot(validate_actions(actions)[1]["severity_seconds"], actions[1]["severity_seconds"])

    def test_reply_options_inherit_independently_without_rewriting_old_actions(self):
        action = {"type": "reply", "mode": "confirm", "template": "notice"}
        self.assertEqual(validate_actions([action]), [action])
        self.assertEqual(resolve_reply_options(action, REPLY_DEFAULTS), {"quote": True, "mention_sender": False})
        override = {**action, "quote": False}
        self.assertEqual(validate_actions([override]), [override])
        self.assertEqual(resolve_reply_options(override, {**REPLY_DEFAULTS, "reply_mention_sender": True}),
                         {"quote": False, "mention_sender": True})

    def test_reject_invalid_actions(self):
        bad = [[{"type": "kick", "mode": "auto"}],
               [{"type": "recall", "mode": "auto"}, {"type": "recall", "mode": "confirm"}],
               [{"type": "recall", "mode": "other"}],
               [{"type": "reply", "mode": "auto"}],
               [{"type": "reply", "mode": "auto", "template": "{sender_id.__class__}"}],
               [{"type": "reply", "mode": "auto", "template": "{rule_id!r}"}],
               [{"type": "reply", "mode": "auto", "template": "ok", "quote": 1}],
               [{"type": "reply", "mode": "auto", "template": "ok", "mention_sender": "false"}],
               [{"type": "recall", "mode": "auto", "quote": True}],
               [{"type": "mute", "mode": "auto", "duration_mode": "strict", "severity_seconds": {}}],
               [{"type": "mute", "mode": "auto", "duration_mode": "autonomous", "severity_seconds": {"low": 10}}]]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_actions(value)
