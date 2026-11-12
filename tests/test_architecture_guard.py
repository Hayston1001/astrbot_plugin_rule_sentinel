import ast
import unittest
from pathlib import Path


class ArchitectureGuardTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent.parent
        self.tree = ast.parse((root / "main.py").read_text(encoding="utf-8"))
        self.text = (root / "main.py").read_text(encoding="utf-8")

    def listeners(self):
        found = []
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if "event_message_type" in ast.unparse(decorator):
                        found.append(node)
        return found

    def test_only_the_passive_group_listener_is_registered(self):
        listeners = self.listeners()
        self.assertEqual([node.name for node in listeners], ["capture_group_message"])
        handler = listeners[0]
        self.assertIsInstance(handler, ast.AsyncFunctionDef)
        self.assertIn("GROUP_MESSAGE", ast.unparse(handler.decorator_list[0]))

    def test_listener_never_sends_stops_or_mutates_the_event(self):
        handler = self.listeners()[0]
        source = ast.unparse(handler)
        for forbidden in ("stop_event", "set_result", "plain_result", "call_llm",
                          "send(", "yield", ".message_obj."):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_entrypoint_has_no_real_moderation_action(self):
        self.assertNotIn("restrict_chat_setting", self.text)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                self.assertNotIn("PlatformTap", ast.unparse(node.func))

    def test_experimental_guarded_invoker_is_not_enabled_by_runtime(self):
        root = Path(__file__).resolve().parent.parent
        for path in (root / "main.py", root / "sentinel" / "runtime_factory.py"):
            with self.subTest(path=path.name):
                self.assertNotIn("guarded_openai_invoker", path.read_text(encoding="utf-8"))
                self.assertNotIn("GuardedOpenAIInvoker", path.read_text(encoding="utf-8"))
                self.assertNotIn("provider_binding", path.read_text(encoding="utf-8"))
