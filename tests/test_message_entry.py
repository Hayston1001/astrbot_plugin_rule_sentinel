import sys
import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path
import types
import unittest
from unittest.mock import patch

from sentinel.message_entry import is_activated_command, strip_wake_prefix


class CommandFilter: pass
class CommandGroupFilter: pass
class RegexFilter: pass
class At:
    def __init__(self, qq):
        self.qq = qq


def modules():
    return {f"astrbot.core.star.filter.{name}": types.SimpleNamespace(**{key: value}) for name, key, value in (
        ("command", "CommandFilter", CommandFilter), ("command_group", "CommandGroupFilter", CommandGroupFilter),
        ("regex", "RegexFilter", RegexFilter))}


class MessageEntryTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not installed")
    def test_target_astrbot_filter_types(self):
        code = """
from types import SimpleNamespace
from sentinel.message_entry import is_activated_command
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.regex import RegexFilter
for kind in (CommandFilter, CommandGroupFilter, RegexFilter):
    handler = SimpleNamespace(handler=lambda: None, event_filters=[kind.__new__(kind)])
    assert is_activated_command(SimpleNamespace(get_extra=lambda _: [handler]), lambda: None)
"""
        with tempfile.TemporaryDirectory() as directory:
            root = str(Path(__file__).resolve().parent.parent)
            env = {**os.environ, "ASTRBOT_ROOT": directory,
                   "PYTHONPATH": os.pathsep.join([root, os.environ.get("PYTHONPATH", "")])}
            result = subprocess.run([sys.executable, "-B", "-c", code], cwd=directory, env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_only_activated_command_types_are_skipped(self):
        def own(): pass
        def other(): pass
        with patch.dict(sys.modules, modules()):
            for kind in (CommandFilter, CommandGroupFilter, RegexFilter):
                with self.subTest(kind=kind):
                    own_entry = types.SimpleNamespace(handler=own, event_filters=[kind()])
                    other_entry = types.SimpleNamespace(handler=other, event_filters=[kind()])
                    event = types.SimpleNamespace(get_extra=lambda _: [own_entry])
                    self.assertFalse(is_activated_command(event, own))
                    event.get_extra = lambda _: [own_entry, other_entry]
                    self.assertTrue(is_activated_command(event, own))
            for value in (None, {}, [object()], [types.SimpleNamespace(event_filters="unknown")]):
                self.assertFalse(is_activated_command(types.SimpleNamespace(get_extra=lambda _: value), own))

    def test_import_failure_is_not_a_command(self):
        event = types.SimpleNamespace(get_extra=lambda _: [types.SimpleNamespace(event_filters=[CommandFilter()])])
        with patch.dict(sys.modules, {name: None for name in modules()}):
            self.assertFalse(is_activated_command(event, lambda: None))

    def test_prefixes_and_mentions(self):
        event = types.SimpleNamespace(message_obj=types.SimpleNamespace(message=[]),
                                      unified_msg_origin="p:GroupMessage:g", get_self_id=lambda: "bot")
        seen = []
        def config(*, umo):
            seen.append(umo)
            return {"wake_prefix": ["!!", "/", "!"]}
        context = types.SimpleNamespace(get_config=config)
        for text, expected in (("/ 加微信拉群", "加微信拉群"), ("!! hello", "hello"),
                               ("!hello", "hello"), ("hello / there", "hello / there"),
                               ("/  ", ""), ("ordinary", "ordinary"), ("//text", "/text")):
            with self.subTest(text=text):
                self.assertEqual(strip_wake_prefix(event, context, text, At), expected)
        self.assertTrue(all(umo == "p:GroupMessage:g" for umo in seen))
        event.message_obj.message = [At("bot")]
        self.assertEqual(strip_wake_prefix(event, context, "/ untouched", At), "/ untouched")
        event.message_obj.message = [At("someone-else")]
        self.assertEqual(strip_wake_prefix(event, context, "/ text", At), "text")
        event.message_obj.message = [At("all")]
        self.assertEqual(strip_wake_prefix(event, context, "/ untouched", At), "/ untouched")

    def test_unavailable_configuration_preserves_original(self):
        event = types.SimpleNamespace(message_obj=types.SimpleNamespace(message=[]), unified_msg_origin="p:g")
        self.assertEqual(strip_wake_prefix(event, object(), "/ text", At), "/ text")
