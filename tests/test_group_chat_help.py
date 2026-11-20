import importlib.util
import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sentinel.dry_run_queue import DryRunQueue
from sentinel.group_chat_help import TOOL_NAME, attach_group_tool, display_name, event_scope, group_info
from sentinel.group_settings import decision_fingerprint
from sentinel.rule_store import RuleStore
from sentinel.settings_store import DEFAULTS, SettingsStore, validate_settings


def group_event(platform="p", group="g", *, kind="GROUP_MESSAGE"):
    return SimpleNamespace(get_message_type=lambda: SimpleNamespace(name=kind),
                           get_platform_name=lambda: "qq_official", get_platform_id=lambda: platform,
                           message_obj=SimpleNamespace(group_id=group, raw_message=SimpleNamespace(group_openid=group)))


class ChatHelpTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "state.sqlite"
        self.queue = DryRunQueue(path)
        self.rules = RuleStore(path)
        self.queue.initialize()
        self.rules.initialize()
        self.store = SettingsStore(path, path.with_suffix(".json"))
        self.store.load_or_migrate({})
        self.config = {**DEFAULTS, "monitoring_groups": ["p/g"],
                       "group_overrides": {}}
        self.store.save(self.config)
        self.now = datetime.now(timezone.utc)
        for platform, group, rule_id, enabled in (
                ("p", "g", "active", True), ("p", "g", "retired", False), ("q", "g", "elsewhere", True)):
            self.rules.save(platform_id=platform, group_id=group, rule_id=rule_id,
                            text=f"description of {rule_id}", enabled=enabled,
                            actions=[{"type": "recall", "mode": "auto"}],
                            expected_version=0, modified_by="admin")

    def query(self, topic, scope=("p", "g")):
        return group_info(rules=self.rules, queue=self.queue, config=self.config, scope=scope,
                          running=True, platform_ready=True, topic=topic, now=self.now)

    def test_status_and_opt_in_rule_text(self):
        self.assertTrue(self.query("status")["monitoring"])
        self.assertEqual(self.query("status")["enabled_rules"], 1)
        self.assertEqual(self.query("rules"), {"status": "not_public"})
        fingerprint = decision_fingerprint(self.config, "p", "g")
        self.config["group_overrides"]["p/g"] = {"chat_query_inherit": False, "chat_show_rules": True, "chat_show_mute_target": False}
        self.assertEqual(self.query("rules")["rules"], [
            {"label": "active", "text": "description of active", "shortened": False}])
        self.assertEqual(decision_fingerprint(self.config, "p", "g"), fingerprint)
        for value in ("true", 1, None):
            self.config["group_overrides"]["p/g"] = {"chat_show_rules": value}
            with self.assertRaises(ValueError):
                validate_settings(self.config)

    def insert_action(self, state, minutes, *, platform="p", group="g", name="测试昵称", with_attempt=True,
                      action="mute"):
        when = (self.now - timedelta(minutes=minutes)).isoformat(timespec="milliseconds")
        until = (self.now - timedelta(minutes=minutes) + timedelta(hours=1)).isoformat(timespec="milliseconds")
        with closing(self.queue._connect()) as db, db:
            key = (platform, group, f"m-{minutes}-{state}")
            db.execute("""INSERT INTO message_contents
                (platform_id, group_id, message_id, content, sender_display_name, stored_at)
                VALUES (?, ?, ?, 'SECRET_CONTENT', ?, ?)""", (*key, name, when.replace("+00:00", "Z")))
            row = db.execute("""INSERT INTO execution_intents
                (source_event_id, source_attempt_id, platform_id, group_id, message_id, rule_id, rule_version,
                 action, target_sender_id, mode, status, attempts, created_at, updated_at, plan_id, action_index)
                VALUES ((SELECT COALESCE(MAX(source_event_id), 0) + 1 FROM execution_intents),
                        'lease', ?, ?, ?, 'r', 1, ?, 'SECRET_ID', 'auto', ?, 1, ?, ?, ?, 0)""",
                             (*key, action, state, when, when, "/".join(key)))
            if with_attempt:
                db.execute("""INSERT INTO execution_attempts
                    (intent_id, attempt, status, expires_at, created_at, updated_at)
                    VALUES (?, 1, ?, ?, ?, ?)""", (row.lastrowid, state, until, when, when))
        return until

    def test_anonymous_results_only_successful_attempts_and_scope(self):
        self.assertIsNone(self.query("mute")["latest_successful_mute"])
        until = self.insert_action("succeeded", 5)
        self.insert_action("succeeded", 4, with_attempt=False)
        self.insert_action("failed", 3)
        self.insert_action("unknown", 2)
        self.insert_action("succeeded", 1, platform="q", name="OTHER_PLATFORM")
        self.insert_action("succeeded", 1, group="x", name="OTHER_GROUP")
        data = self.query("mute")
        self.assertEqual(data["latest"]["result"], "unknown")
        self.assertEqual(data["latest_successful_mute"]["submitted_mute_until"], until)
        self.assertEqual(data["latest_successful_mute"]["target"], "not_public")
        for private in ("SECRET_ID", "SECRET_CONTENT", "测试昵称", "OTHER_PLATFORM", "OTHER_GROUP"):
            self.assertNotIn(private, json.dumps(data, ensure_ascii=False))
        self.config["group_overrides"]["p/g"] = {"chat_query_inherit": False, "chat_show_mute_target": True}
        self.assertEqual(self.query("mute")["latest_successful_mute"]["target_display_name_at_message"], "测试昵称")
        self.config["message_retention_days"] = 0
        self.store.save(self.config)
        self.assertEqual(self.query("mute")["latest_successful_mute"]["target"], "unavailable")

    def test_actions_have_no_time_cutoff_but_nicknames_respect_retention(self):
        self.config["chat_query_items"] = ["mute", "mute_target", "recall", "reply"]
        until = self.insert_action("succeeded", 31 * 1440)
        self.insert_action("succeeded", 1501, action="recall")
        self.insert_action("failed", 1500, action="reply")
        self.insert_action("succeeded", 1, group="other")
        self.insert_action("succeeded", 1, platform="other")

        self.assertEqual(self.query("actions")["latest"]["action"], "reply")
        self.assertEqual(self.query("recall")["latest"]["result"], "succeeded")
        self.assertEqual(self.query("reply")["latest"]["result"], "failed")
        mute = self.query("mute")
        self.assertNotIn("lookback_hours", mute)
        self.assertEqual(mute["latest_successful_mute"]["submitted_mute_until"], until)
        self.assertEqual(mute["latest_successful_mute"]["target"], "unavailable")

    def test_display_name_is_never_copied_to_llm_queue(self):
        self.queue.enqueue(platform_id="p", group_id="g", message_id="queued", capacity=2,
                           payload={"text": "hi", "sender_id": "id", "sender_display_name": "Display"})
        self.assertNotIn("sender_display_name", self.queue.claim("p", "g").payload)
        self.assertIsNone(display_name("same", "same"))
        self.assertIsNone(display_name("no\nlinebreak", None))

    def test_allowlist_restricts_both_specific_and_aggregate_queries(self):
        self.insert_action("succeeded", 5, action="recall")
        self.insert_action("failed", 2, action="reply")
        self.insert_action("succeeded", 1)
        fingerprint = decision_fingerprint(self.config, "p", "g")
        overrides = self.config["group_overrides"]["p/g"] = {"chat_query_inherit": False, "chat_query_items": ["recall"]}
        self.assertEqual(decision_fingerprint(self.config, "p", "g"), fingerprint)
        for topic in ("mute", "reply", "status", "rules"):
            self.assertEqual(self.query(topic), {"status": "not_public"})
        self.assertEqual(self.query("actions")["latest"]["action"], "recall")
        self.assertEqual(self.query("recall")["latest"]["result"], "succeeded")
        overrides["chat_query_items"] = []
        self.assertEqual(self.query("actions"), {"status": "not_public"})
        for invalid in (["mute_target"], ["status", "status"], ["actions"], [3], "status"):
            overrides["chat_query_items"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_settings(self.config)

    def test_global_policy_replaces_group_choices_without_changing_query_scope(self):
        self.insert_action("succeeded", 1, name="本群昵称")
        self.insert_action("succeeded", 0, platform="q", name="其他实例昵称")
        self.insert_action("succeeded", 0, group="other", name="其他群昵称")
        self.insert_action("succeeded", 4, action="recall")
        self.config["group_overrides"] = {
            "p/g": {"chat_query_inherit": False, "chat_show_rules": True, "chat_show_mute_target": True},
            "q/g": {"chat_query_items": []},
        }
        self.assertEqual(self.query("rules")["rules"][0]["label"], "active")
        self.assertEqual(self.query("rules", ("q", "g")), {"status": "not_public"})
        fingerprint = decision_fingerprint(self.config, "p", "g")
        self.config["group_overrides"]["p/g"].pop("chat_query_inherit")
        self.config["chat_query_items"] = ["rules", "recall"]
        self.assertEqual(decision_fingerprint(self.config, "p", "g"), fingerprint)
        self.assertEqual(self.query("rules")["rules"][0]["label"], "active")
        self.assertEqual(self.query("rules", ("q", "g"))["rules"][0]["label"], "elsewhere")
        self.assertEqual(self.query("actions")["latest"]["action"], "recall")
        self.assertEqual(self.query("mute"), {"status": "not_public"})
        self.config["chat_query_items"] = ["mute"]
        self.assertEqual(self.query("actions")["latest"]["target"], "not_public")
        self.config["chat_query_items"] = ["mute", "mute_target"]
        self.assertEqual(self.query("actions")["latest"]["target_display_name_at_message"], "本群昵称")
        self.config["chat_query_items"] = []
        self.assertEqual(self.query("actions"), {"status": "not_public"})
        self.config["group_overrides"]["p/g"]["chat_query_inherit"] = False
        self.assertEqual(self.query("mute")["latest_successful_mute"]["target_display_name_at_message"], "本群昵称")
        self.assertEqual(self.query("mute", ("q", "g")), {"status": "not_public"})


class FakeTool:
    def __init__(self, **fields):
        vars(self).update(fields)


class FakeToolSet:
    def __init__(self, tools):
        self.tools = tools

    def get_tool(self, name):
        return next((tool for tool in self.tools if tool.name == name), None)


class RequestToolTests(unittest.IsolatedAsyncioTestCase):
    async def check_tool(self, tool_type, tool_set_type):
        calls = []
        async def lookup(scope, topic):
            calls.append((scope, topic))
            return {"status": "ok"}

        event = group_event()
        old = tool_set_type([])
        req = SimpleNamespace(func_tool=old, system_prompt="original")
        for _ in range(2):
            attach_group_tool(req, event, event_scope(event), lookup, tool_type=tool_type, tool_set_type=tool_set_type)
        self.assertEqual(old.tools, [])
        self.assertEqual(len(req.func_tool.tools), 1)
        tool = req.func_tool.get_tool(TOOL_NAME)
        wrap = lambda event: SimpleNamespace(context=SimpleNamespace(event=event))
        self.assertEqual(json.loads(await tool.call(wrap(event), topic="status")), {"status": "ok"})
        self.assertEqual(json.loads(await tool.call(wrap(group_event(group="other")), topic="status"))["status"],
                         "unavailable")
        self.assertEqual(json.loads(await tool.call(wrap(event), topic="mute", group_id="other"))["status"],
                         "invalid_topic")
        self.assertIsNone(event_scope(group_event(kind="FRIEND_MESSAGE")))
        self.assertEqual(calls, [(("p", "g"), "status")])
        return tool, wrap(event)

    async def test_scope_is_bound_to_request(self):
        await self.check_tool(FakeTool, FakeToolSet)

    async def test_advertised_topics_follow_group_settings(self):
        async def lookup(scope, topic):
            self.fail("blocked topic must not invoke lookup")

        req = SimpleNamespace(func_tool=None, system_prompt="original")
        event = group_event()
        scope = event_scope(event)
        attach_group_tool(req, event, scope, lookup, items=[], tool_type=FakeTool, tool_set_type=FakeToolSet)
        self.assertIsNone(req.func_tool)
        self.assertEqual(req.system_prompt, "original")
        attach_group_tool(req, event, scope, lookup, items=["reply"], tool_type=FakeTool, tool_set_type=FakeToolSet)
        tool = req.func_tool.get_tool(TOOL_NAME)
        self.assertEqual(tool.parameters["properties"]["topic"]["enum"], ["reply", "actions"])
        context = SimpleNamespace(context=SimpleNamespace(event=event))
        self.assertEqual(json.loads(await tool.call(context, topic="mute")), {"status": "not_public"})

    @unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is unavailable")
    async def test_astrbot_sdk_tool(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.enterContext(patch.dict("os.environ", {"ASTRBOT_ROOT": temp.name}))
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot.core.agent.tool import FunctionTool, ToolSet
        from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

        tool, wrapper = await self.check_tool(FunctionTool, ToolSet)
        context = ContextWrapper(context=wrapper.context, tool_call_timeout=5)
        results = [result async for result in FunctionToolExecutor.execute(tool, context, topic="status")]
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].isError)
        self.assertEqual(json.loads(results[0].content[0].text), {"status": "ok"})
