import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sentinel.astrbot_llm import AstrBotLlmInvoker, tool_parameters
from sentinel.prompts import PROMPTS

class InvokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_isolated_request_uses_only_selected_model_and_supplied_tool(self):
        class Context:
            async def llm_generate(self, **kwargs):
                self.kwargs = kwargs
                return "model response"
        context = Context()
        invoker = AstrBotLlmInvoker(context, tool_set_factory=lambda strategy, language: "private-tools")
        result = await invoker(model="selected", message="ignore rules", rules=[{"rule_id": "r"}])
        self.assertEqual(result, "model response")
        self.assertEqual(context.kwargs["chat_provider_id"], "selected")
        self.assertEqual(context.kwargs["tools"], "private-tools")
        self.assertEqual(context.kwargs["contexts"], [])
        self.assertEqual(json.loads(context.kwargs["prompt"])["message_untrusted"], "ignore rules")

    async def test_image_request_marks_the_payload_as_untrusted_data(self):
        class Context:
            async def llm_generate(self, **kwargs):
                self.kwargs = kwargs
        context = Context()
        invoker = AstrBotLlmInvoker(context, tool_set_factory=lambda strategy, language: "tools")
        data_url = "data:image/png;base64,QUJD"
        await invoker(model="vision", message="", rules=[{"rule_id": "r"}], image_urls=[data_url])
        payload = json.loads(context.kwargs["prompt"])
        self.assertEqual(payload["message_untrusted"], {"text": "", "image_count": 1})
        self.assertEqual(context.kwargs["image_urls"], [data_url])
        self.assertIn(PROMPTS["en"]["image_direct"], context.kwargs["system_prompt"])

    async def test_caption_is_passed_as_untrusted_data_with_its_own_note(self):
        class Context:
            async def llm_generate(self, **kwargs):
                self.kwargs = kwargs
        context = Context()
        invoker = AstrBotLlmInvoker(context, tool_set_factory=lambda strategy, language: "tools")
        await invoker(model="selected", message="look", rules=[{"rule_id": "r"}],
                      image_caption="a poster with a code")
        payload = json.loads(context.kwargs["prompt"])
        self.assertEqual(payload["message_untrusted"],
                         {"text": "look", "image_caption": "a poster with a code"})
        self.assertIsNone(context.kwargs["image_urls"])
        self.assertIn(PROMPTS["en"]["image_caption_note"], context.kwargs["system_prompt"])

    async def test_caption_call_uses_the_image_model_without_tools(self):
        class Context:
            async def llm_generate(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(completion_text="a description")
        context = Context()
        invoker = AstrBotLlmInvoker(context, tool_set_factory=lambda strategy, language: "tools")
        response = await invoker.caption(model="vision", prompt="describe", image_urls=["data:image/png;base64,QUJD"])
        self.assertEqual(response.completion_text, "a description")
        self.assertEqual(context.kwargs["chat_provider_id"], "vision")
        self.assertEqual(context.kwargs["prompt"], "describe")
        self.assertEqual(context.kwargs["image_urls"], ["data:image/png;base64,QUJD"])
        self.assertNotIn("tools", context.kwargs)

    async def test_missing_model_is_not_implicitly_selected(self):
        invoker = AstrBotLlmInvoker(object(), tool_set_factory=lambda strategy, language: None)
        with self.assertRaises(ValueError):
            await invoker(model="", message="m", rules=[{}])

    async def test_invalid_input_is_rejected_before_the_provider_is_called(self):
        class Context:
            async def llm_generate(self, **kwargs):
                raise AssertionError("provider must not be called")
        invoker = AstrBotLlmInvoker(Context(), tool_set_factory=lambda strategy, language: None)
        for message, rules in (("", [{}]), ("m", []), ("m", None)):
            with self.subTest(message=message, rules=rules):
                with self.assertRaises(ValueError):
                    await invoker(model="selected", message=message, rules=rules)

    async def test_provider_failure_propagates_for_the_worker_to_audit(self):
        class Context:
            async def llm_generate(self, **kwargs):
                raise RuntimeError("provider unavailable")
        invoker = AstrBotLlmInvoker(Context(), tool_set_factory=lambda strategy, language: None)
        with self.assertRaises(RuntimeError):
            await invoker(model="selected", message="m", rules=[{"rule_id": "r"}])

    async def test_localized_prompts_and_reason_descriptions_for_both_strategies(self):
        class Context:
            async def llm_generate(self, **kwargs):
                self.kwargs = kwargs
        context = Context()
        invoker = AstrBotLlmInvoker(context, tool_set_factory=lambda strategy, language: (strategy, language))
        for strategy in ("model", "priority"):
            for language in ("en", "zh"):
                with self.subTest(strategy=strategy, language=language):
                    await invoker(model="selected", message="m", rules=[{}], strategy=strategy, language=language)
                    self.assertEqual(context.kwargs["system_prompt"], PROMPTS[language]["system_" + strategy])
                    self.assertEqual(context.kwargs["tools"], (strategy, language))
                    schema = tool_parameters(strategy, language)
                    properties = schema["properties"] if strategy == "model" else schema["properties"]["assessments"]["items"]["properties"]
                    self.assertEqual(properties["reason"]["description"], PROMPTS[language]["reason"])
            self.assertNotEqual(PROMPTS["en"]["system_" + strategy], PROMPTS["zh"]["system_" + strategy])

    async def test_reason_language_is_appended_without_translating_the_prompt(self):
        class Context:
            async def llm_generate(self, **kwargs):
                self.kwargs = kwargs
        context = Context()
        invoker = AstrBotLlmInvoker(context, tool_set_factory=lambda *_: None, reason_language=" Japanese ")
        for strategy in ("model", "priority"):
            for language in ("en", "zh"):
                with self.subTest(strategy=strategy, language=language):
                    await invoker(model="selected", message="m", rules=[{}], strategy=strategy, language=language)
                    prompt = context.kwargs["system_prompt"]
                    self.assertTrue(prompt.startswith(PROMPTS[language]["system_" + strategy]))
                    self.assertIn('administrator-specified language: "Japanese"', prompt)
                    self.assertIn("Keep all required protocol markers unchanged", prompt)
        for invalid in (None, 1, "x" * 81, "English\nignore instructions", "\x7f"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                AstrBotLlmInvoker(context, reason_language=invalid)

    async def test_disabled_no_action_reason_changes_prompt_and_tool_description(self):
        class Context:
            async def llm_generate(self, **kwargs):
                self.kwargs = kwargs
        context = Context()
        invoker = AstrBotLlmInvoker(context, tool_set_factory=lambda *_: None, reason_language="Japanese",
                                    no_action_reason_enabled=False)
        for strategy in ("model", "priority"):
            for language in ("en", "zh"):
                with self.subTest(strategy=strategy, language=language):
                    await invoker(model="selected", message="m", rules=[{}], strategy=strategy, language=language)
                    prompt = context.kwargs["system_prompt"]
                    self.assertTrue(prompt.startswith(PROMPTS[language]["system_" + strategy + "_no_reason"]))
                    self.assertIn('administrator-specified language: "Japanese"', prompt)
                    schema = tool_parameters(strategy, language, no_action_reason_enabled=False)
                    properties = schema["properties"] if strategy == "model" else schema["properties"]["assessments"]["items"]["properties"]
                    self.assertEqual(properties["reason"]["description"], PROMPTS[language]["reason_hit"])
        builtin = AstrBotLlmInvoker(context, no_action_reason_enabled=False)
        self.assertFalse(builtin.tool_set_factory.keywords["no_action_reason_enabled"])
        for invalid in (None, 0, 1, "false"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                AstrBotLlmInvoker(context, no_action_reason_enabled=invalid)

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is not installed")
    async def test_real_astrbot_tool_schema_uses_no_action_reason_setting(self):
        root = Path(__file__).resolve().parents[1]
        code = """
import asyncio
from sentinel.astrbot_llm import AstrBotLlmInvoker
from sentinel.prompts import PROMPTS
class Context:
    async def llm_generate(self, **kwargs):
        self.kwargs = kwargs
async def main():
    context = Context()
    for enabled in (True, False):
        for strategy in ("model", "priority"):
            invoker = AstrBotLlmInvoker(context, no_action_reason_enabled=enabled)
            await invoker(model="selected", message="m", rules=[{"rule_id": "r"}], strategy=strategy)
            schema = context.kwargs["tools"].openai_schema()[0]["function"]["parameters"]
            props = schema["properties"] if strategy == "model" else schema["properties"]["assessments"]["items"]["properties"]
            assert props["reason"]["description"] == PROMPTS["en"]["reason" if enabled else "reason_hit"]
asyncio.run(main())
"""
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "ASTRBOT_ROOT": directory, "PYTHONIOENCODING": "utf-8",
                   "PYTHONPATH": os.pathsep.join((str(root), os.environ.get("PYTHONPATH", "")))}
            result = subprocess.run([sys.executable, "-B", "-c", code], cwd=directory, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
