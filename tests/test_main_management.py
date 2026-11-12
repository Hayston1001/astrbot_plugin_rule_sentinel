"""Mocked plugin wiring tests; these do not load a running AstrBot instance."""

import importlib
import importlib.util
import asyncio
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch
from test_message_entry import CommandFilter, CommandGroupFilter, RegexFilter, modules as command_modules


class MainManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(__file__).resolve().parent.parent
        package = types.ModuleType("sentinel_test_plugin")
        package.__path__ = [str(root)]
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        api.AstrBotConfig = dict
        api.logger = types.SimpleNamespace(info=lambda *args: None, error=lambda *args: None, warning=lambda *args: None)
        star = types.ModuleType("astrbot.api.star")
        star.Context = object
        class Star:
            def __init__(self, context):
                self.context = context
        star.Star = Star
        event = types.ModuleType("astrbot.api.event")
        event.AstrMessageEvent = object
        event.filter = types.SimpleNamespace(EventMessageType=types.SimpleNamespace(GROUP_MESSAGE="group"),
                                             event_message_type=lambda *_args, **_kwargs: lambda handler: handler)
        components = types.ModuleType("astrbot.api.message_components")
        components.At = type("At", (), {})
        components.Plain = type("Plain", (), {})
        web = types.ModuleType("astrbot.api.web")
        self.request = types.SimpleNamespace(username="admin", query={}, headers={"content-type": "application/json"})
        async def body():
            return self.raw_body
        self.request.body = body
        self.raw_body = b"{}"
        web.request = self.request
        web.error_response = lambda message, status_code=400: {"error": message, "status_code": status_code}
        web.json_response = lambda data: {"data": data}
        core = types.ModuleType("astrbot.core")
        utils = types.ModuleType("astrbot.core.utils")
        paths = types.ModuleType("astrbot.core.utils.astrbot_path")
        paths.get_astrbot_plugin_data_path = lambda: self.temp.name
        modules = {"sentinel_test_plugin": package, "astrbot": astrbot, "astrbot.api": api,
                   "astrbot.api.star": star, "astrbot.api.web": web, "astrbot.api.event": event,
                   "astrbot.api.message_components": components, "astrbot.core": core,
                   "astrbot.core.utils": utils, "astrbot.core.utils.astrbot_path": paths}
        self.patch = patch.dict(sys.modules, modules)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(lambda: [sys.modules.pop(name, None) for name in list(sys.modules)
                                 if name.startswith("sentinel_test_plugin.")])
        self.module = importlib.import_module("sentinel_test_plugin.main")
        class Context:
            def __init__(self):
                self.routes = []
                self.registered_web_apis = self.routes
                official = types.SimpleNamespace(meta=lambda: types.SimpleNamespace(name="qq_official", id="qq1"))
                telegram = types.SimpleNamespace(meta=lambda: types.SimpleNamespace(name="telegram", id="tg"))
                self.platform_manager = types.SimpleNamespace(get_insts=lambda: [official, telegram])
            def register_web_api(self, *args):
                self.routes.append(args)
        self.context = Context()
        self.plugin = self.module.RuleSentinel(self.context, {"dashboard_admins": ["admin"]})
        await self.plugin.initialize()

    async def test_routes_and_confirmed_rule(self):
        self.assertEqual(len(self.context.routes), 26)
        self.assertEqual((await self.plugin.list_platforms())["data"], [{"id": "qq1", "type": "qq_official"}])
        fields = dict(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                      text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                      severity_seconds={"low": 30})
        self.raw_body = json.dumps(fields).encode()
        preview = (await self.plugin.preview_rule())["data"]
        self.raw_body = json.dumps(preview).encode()
        result = await self.plugin.confirm_rule()
        self.assertEqual(result["data"]["modified_by"], "admin")
        self.request.query = {"platform_id": "p", "group_id": "g"}
        self.assertEqual(len((await self.plugin.list_rules())["data"]), 1)
        self.request.query["rule_id"] = "r"
        self.assertEqual((await self.plugin.rule_history())["data"][0]["version"], 1)

    async def test_action_rules_and_execution_status(self):
        fields = {"platform_id": "p", "group_id": "g", "rule_id": "reply", "expected_version": 0,
                  "text": "rule", "enabled": True, "actions": [
                      {"type": "reply", "mode": "auto", "template": "{sender_id}: {rule_id}"},
                      {"type": "recall", "mode": "confirm"}]}
        self.raw_body = json.dumps(fields).encode()
        preview = await self.plugin.preview_rule()
        self.assertIn("data", preview)
        self.raw_body = json.dumps(preview["data"]).encode()
        saved = await self.plugin.confirm_rule()
        self.assertEqual(saved["data"]["actions"], fields["actions"])
        status = (await self.plugin.status())["data"]
        self.assertTrue(status["dry_run_only"])
        self.assertTrue(status["action_execution_enabled"])
        self.assertFalse(status["action_execution_available"])
        fields["actions"] = [{"type": "mute", "mode": "auto", "duration_mode": "strict",
                              "severity_seconds": {"low": 2592001}}]
        self.raw_body = json.dumps(fields).encode()
        self.assertEqual((await self.plugin.preview_rule())["status_code"], 400)

    async def test_mute_confirmation_accepts_only_id_and_dashboard_user(self):
        host = types.SimpleNamespace(mute_executor=object(), confirm_mute=AsyncMock(return_value="succeeded"))
        self.plugin.host = host
        self.plugin.config["action_execution_enabled"] = True
        try:
            for body in ({"intent_id": 2, "target_sender_id": "other"}, {"intent_id": True}, {}, {"intent_id": -1}):
                self.raw_body = json.dumps(body).encode()
                self.assertEqual((await self.plugin.execute_mute())["status_code"], 400)
            self.raw_body = b'{"intent_id": 2}'
            self.request.username = "api_key:synthetic"
            self.assertEqual((await self.plugin.execute_mute())["status_code"], 403)
            self.request.username = "admin"
            self.assertEqual((await self.plugin.execute_mute())["data"]["status"], "succeeded")
            host.confirm_mute.assert_awaited_once_with(2)
        finally:
            self.plugin.host = None

    async def test_jev_models_authorization_and_secrets(self):
        self.plugin.config["jev_api_key"] = "saved-secret"
        self.raw_body = b'{"provider":"openrouter"}'
        self.request.username = "api_key:synthetic"
        self.assertEqual((await self.plugin.list_jev_models())["status_code"], 403)
        self.request.username = "admin"
        result = await self.plugin.list_jev_models()
        self.assertEqual(result["data"]["models"], ["typesafe/jev-latest"])
        self.assertNotIn("saved-secret", repr(result))
        self.raw_body = b'{"provider":"openrouter","api_key":"temporary-secret"}'
        result = await self.plugin.list_jev_models()
        self.assertNotIn("temporary-secret", repr(result))
        self.assertEqual(self.plugin.config["jev_api_key"], "saved-secret")
        self.raw_body = b'{"provider":"typesafe","api_key":"temporary-secret"}'
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
        fake_aiohttp = types.SimpleNamespace(ClientSession=lambda **kwargs: Session())
        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}), patch.object(self.module, "list_jev_models", new_callable=AsyncMock) as discover:
            discover.side_effect = RuntimeError("temporary-secret")
            with patch.object(self.module.logger, "error") as logged:
                result = await self.plugin.list_jev_models()
            self.assertEqual(result["status_code"], 502)
            self.assertNotIn("temporary-secret", repr(result))
            logged.assert_not_called()
            self.assertEqual(discover.await_args.args, ("typesafe", "temporary-secret"))
        self.assertEqual(self.plugin.config["jev_api_key"], "saved-secret")

    async def test_dashboard_users_allowed_but_api_key_and_anonymous_denied(self):
        self.request.query = {"platform_id": "p", "group_id": "g"}
        self.plugin.config["dashboard_admins"] = {"invalid": "legacy value ignored"}
        self.request.username = "member"
        self.assertEqual((await self.plugin.list_rules())["data"], [])
        self.assertEqual((await self.plugin.list_groups())["data"], [])
        for username in ("api_key:123", None, ""):
            self.request.username = username
            self.assertEqual((await self.plugin.list_rules())["status_code"], 403)
            self.assertEqual((await self.plugin.rule_history())["status_code"], 403)
            self.assertEqual((await self.plugin.preview_rule())["status_code"], 403)
            self.assertEqual((await self.plugin.status())["status_code"], 403)
            self.assertEqual((await self.plugin.list_notifications())["status_code"], 403)
            self.assertEqual((await self.plugin.list_platforms())["status_code"], 403)
            self.assertEqual((await self.plugin.list_groups())["status_code"], 403)
            self.assertEqual((await self.plugin.register_group())["status_code"], 403)
            self.assertEqual((await self.plugin.save_group_alias())["status_code"], 403)
            self.assertEqual((await self.plugin.get_group_name())["status_code"], 403)
            self.assertEqual((await self.plugin.list_messages())["status_code"], 403)
            self.assertEqual((await self.plugin.stream_events())["status_code"], 403)
            self.assertEqual((await self.plugin.get_ui_state())["status_code"], 403)
            self.assertEqual((await self.plugin.save_ui_state())["status_code"], 403)
            self.assertEqual((await self.plugin.summary())["status_code"], 403)
            self.assertEqual((await self.plugin.get_settings())["status_code"], 403)
            self.assertEqual((await self.plugin.save_settings())["status_code"], 403)
            self.assertEqual((await self.plugin.set_group_monitoring())["status_code"], 403)
            self.assertEqual((await self.plugin.save_group_settings())["status_code"], 403)
            self.assertEqual((await self.plugin.list_providers())["status_code"], 403)

    async def test_group_settings_register_monitoring_and_platform_isolation(self):
        self.raw_body = json.dumps({"platform_id": "p1", "group_id": "g"}).encode()
        self.assertEqual((await self.plugin.register_group())["data"]["group_id"], "g")
        self.raw_body = json.dumps({"platform_id": "p1", "group_id": "g", "name": "group"}).encode()
        await self.plugin.save_group_alias()
        self.raw_body = json.dumps({"platform_id": "p1", "group_id": "g"}).encode()
        self.assertEqual((await self.plugin.register_group())["data"]["alias"], "group")
        self.raw_body = json.dumps({"platform_id": "p2", "group_id": "g", "overrides": {
            "model": {"backend": "llm", "model": "local"}, "strategy": "priority", "execution_mode": "record",
            "llm_reason_language": "English"}}).encode()
        result = (await self.plugin.save_group_settings())["data"]["settings"]
        self.assertEqual(result["group_overrides"]["p2/g"]["model"]["model"], "local")
        self.assertEqual(result["group_overrides"]["p2/g"]["llm_reason_language"], "English")
        self.assertNotIn("p1/g", result["group_overrides"])
        self.assertNotIn("jev_api_key", result)
        self.raw_body = json.dumps({"platform_id": "p2", "group_id": "g", "enabled": True}).encode()
        self.assertIn("p2/g", (await self.plugin.set_group_monitoring())["data"]["settings"]["monitoring_groups"])
        self.raw_body = json.dumps({"platform_id": "p2", "group_id": "g", "enabled": False}).encode()
        self.assertNotIn("p2/g", (await self.plugin.set_group_monitoring())["data"]["settings"]["monitoring_groups"])
        self.raw_body = json.dumps({"platform_id": "p2", "group_id": "g", "overrides": {}}).encode()
        self.assertEqual((await self.plugin.save_group_settings())["data"]["settings"]["group_overrides"], {})
        groups = (await self.plugin.list_groups())["data"]
        self.assertEqual({(item["platform_id"], item["group_id"]) for item in groups}, {("p1", "g"), ("p2", "g")})
        for overrides in ({"model": None}, {"execution_mode": "invalid"}, {"protected_quote": 1},
                          {"llm_reason_language": None}, {"llm_reason_language": "English\ninvalid"}, {"other": True}):
            self.raw_body = json.dumps({"platform_id": "p1", "group_id": "g", "overrides": overrides}).encode()
            self.assertEqual((await self.plugin.save_group_settings())["status_code"], 400)
        self.raw_body = b'{"platform_id":"p1","group_id":"g","overrides":{},"overrides":{}}'
        self.assertEqual((await self.plugin.save_group_settings())["status_code"], 400)
        self.raw_body = json.dumps({"platform_id": "__rule_sentinel_system__", "group_id": "g"}).encode()
        self.assertEqual((await self.plugin.register_group())["status_code"], 400)

    async def test_no_action_reason_setting_roundtrip_and_invalid_input(self):
        self.assertTrue((await self.plugin.get_settings())["data"]["llm_no_action_reason_enabled"])
        self.raw_body = b'{"llm_no_action_reason_enabled":false}'
        result = await self.plugin.save_settings()
        self.assertFalse(result["data"]["settings"]["llm_no_action_reason_enabled"])
        self.assertFalse((await self.plugin.get_settings())["data"]["llm_no_action_reason_enabled"])
        for value in (None, 0, 1, "false"):
            self.raw_body = json.dumps({"llm_no_action_reason_enabled": value}).encode()
            self.assertEqual((await self.plugin.save_settings())["status_code"], 400)
        self.assertFalse(self.plugin.config["llm_no_action_reason_enabled"])

    async def test_event_stream_invalid_scope_and_limit(self):
        for query in ({}, {"platform_id": "p", "group_id": " "},
                      {"platform_id": "x" * 129, "group_id": "g"}):
            self.request.query = query
            self.assertEqual((await self.plugin.stream_events())["status_code"], 400)
        for _ in range(4):
            self.plugin._event_hub.subscribe("p", "g")
        self.request.query = {"platform_id": "p", "group_id": "g"}
        self.assertEqual((await self.plugin.stream_events())["status_code"], 429)
        self.plugin._event_hub.close()

    @unittest.skipUnless(importlib.util.find_spec("starlette"), "Starlette is unavailable")
    async def test_event_stream_disconnect_cleans_subscription(self):
        self.request.query = {"platform_id": "p", "group_id": "g"}
        response = await self.plugin.stream_events()
        self.assertEqual(len(self.plugin._event_hub.subscribers), 1)
        async def receive():
            return {"type": "http.disconnect"}
        async def send(message):
            pass
        await response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
        self.assertEqual(len(self.plugin._event_hub.subscribers), 0)

    async def test_ui_state_round_trip_is_user_scoped_and_survives_reload(self):
        self.assertIsNone((await self.plugin.get_ui_state())["data"])
        first = {"platform_id": "p", "group_id": "g"}
        self.raw_body = json.dumps(first).encode()
        self.assertEqual((await self.plugin.save_ui_state())["data"], first)
        self.request.username = "other"
        self.assertIsNone((await self.plugin.get_ui_state())["data"])
        second = {"platform_id": "other-platform", "group_id": "other-group"}
        self.raw_body = json.dumps(second).encode()
        self.assertEqual((await self.plugin.save_ui_state())["data"], second)
        self.request.username = "admin"
        self.assertEqual((await self.plugin.get_ui_state())["data"], first)
        other_plugin = self.module.RuleSentinel(self.context, {})
        await other_plugin.initialize()
        try:
            self.assertEqual((await other_plugin.get_ui_state())["data"], first)
        finally:
            await other_plugin.terminate()

    async def test_group_names_are_scoped_persistent_and_validated(self):
        self.raw_body = json.dumps({"platform_id": "qq1", "group_id": "g", "name": "测试群"}).encode()
        result = (await self.plugin.save_group_alias())["data"]
        self.assertEqual(result["alias"], "测试群")
        self.raw_body = json.dumps({"platform_id": "other", "group_id": "g", "name": "另一个群"}).encode()
        await self.plugin.save_group_alias()
        groups = (await self.plugin.list_groups())["data"]
        self.assertEqual({(item["platform_id"], item["alias"]) for item in groups},
                         {("qq1", "测试群"), ("other", "另一个群")})
        for invalid in ({"platform_id": "qq1", "group_id": "g", "name": "x" * 101},
                        {"platform_id": "qq1", "group_id": "g", "name": "bad\nname"},
                        {"platform_id": "qq1", "group_id": "g", "name": "x", "username": "other"}):
            self.raw_body = json.dumps(invalid).encode()
            self.assertEqual((await self.plugin.save_group_alias())["status_code"], 400)
        self.raw_body = b'{"platform_id":"qq1","group_id":"g","name":"a","name":"b"}'
        self.assertEqual((await self.plugin.save_group_alias())["status_code"], 400)
        self.raw_body = json.dumps({"platform_id": "qq1", "group_id": "g", "name": ""}).encode()
        self.assertEqual((await self.plugin.save_group_alias())["data"]["alias"], "")
        other = self.module.RuleSentinel(self.context, {})
        await other.initialize()
        try:
            self.assertEqual(len((await other.list_groups())["data"]), 2)
        finally:
            await other.terminate()

    async def test_group_name_lookup_uses_official_platform_and_preserves_alias(self):
        self.raw_body = json.dumps({"platform_id": "qq1", "group_id": "g", "name": "备注"}).encode()
        await self.plugin.save_group_alias()
        self.raw_body = json.dumps({"platform_id": "qq1", "group_id": "g"}).encode()
        request_name = AsyncMock(return_value={"group_name": "QQ 群名"})
        official = types.SimpleNamespace(meta=lambda: types.SimpleNamespace(name="qq_official", id="qq1"),
                                         get_client=lambda: types.SimpleNamespace(api=types.SimpleNamespace(
                                             _http=types.SimpleNamespace(request=request_name))))
        self.context.platform_manager.get_insts = lambda: [official]
        botpy = types.ModuleType("botpy")
        http = types.ModuleType("botpy.http")
        http.Route = lambda method, path, **kwargs: (method, path, kwargs)
        with patch.dict(sys.modules, {"botpy": botpy, "botpy.http": http}):
            result = (await self.plugin.get_group_name())["data"]
        self.assertEqual((result["alias"], result["platform_name"]), ("备注", "QQ 群名"))
        request_name.assert_awaited_once_with(("GET", "/v2/groups/{group_openid}/info", {"group_openid": "g"}))
        request_name.side_effect = RuntimeError("private platform error")
        with patch.dict(sys.modules, {"botpy": botpy, "botpy.http": http}):
            self.assertEqual((await self.plugin.get_group_name())["status_code"], 502)
        self.assertEqual((await self.plugin.list_groups())["data"][0]["platform_name"], "QQ 群名")
        for group_id in ("../other", "a/b", "a%2fb", "a?x=y", "..", "x" * 129):
            self.raw_body = json.dumps({"platform_id": "qq1", "group_id": group_id}).encode()
            self.assertEqual((await self.plugin.get_group_name())["status_code"], 400)

    async def test_ui_state_rejects_invalid_payload_and_overlong_ids(self):
        cases = ({}, {"platform_id": "p"}, {"platform_id": "p", "group_id": "g", "username": "other"},
                  {"platform_id": " ", "group_id": "g"}, {"platform_id": "p", "group_id": None},
                  {"platform_id": "", "group_id": "g"}, {"platform_id": "p", "group_id": ""},
                  {"platform_id": "p" * 129, "group_id": "g"}, {"platform_id": "p", "group_id": "g" * 129})
        for body in cases:
            with self.subTest(body=body):
                self.raw_body = json.dumps(body).encode()
                self.assertEqual((await self.plugin.save_ui_state())["status_code"], 400)
        self.raw_body = b'{"platform_id":"p","platform_id":"p","group_id":"g"}'
        self.assertEqual((await self.plugin.save_ui_state())["status_code"], 400)
        self.raw_body = b"x" * (self.module.MAX_BODY_BYTES + 1)
        self.assertEqual((await self.plugin.save_ui_state())["status_code"], 400)
        self.raw_body = b'{"platform_id":' + b'[' * 65 + b'0' + b']' * 65 + b',"group_id":"g"}'
        self.assertEqual((await self.plugin.save_ui_state())["status_code"], 400)
        self.raw_body = b'{"platform_id":"p","group_id":"g"}'
        self.request.headers = {"content-type": "text/plain"}
        self.assertEqual((await self.plugin.save_ui_state())["status_code"], 400)
        self.request.headers = {"content-type": "application/json"}
        self.raw_body = json.dumps({"platform_id": "p" * 128, "group_id": "g" * 128}).encode()
        self.assertEqual((await self.plugin.save_ui_state())["data"]["group_id"], "g" * 128)
        self.raw_body = b'{"platform_id":"","group_id":""}'
        self.assertEqual((await self.plugin.save_ui_state())["data"], {"platform_id": "", "group_id": ""})
        self.assertEqual((await self.plugin.get_ui_state())["data"], {"platform_id": "", "group_id": ""})

    async def test_all_groups_records_keep_scope_pagination_and_exclude_system(self):
        queue = self.plugin.management.queue
        for platform, group, message in (("p1", "g", "same"), ("p2", "g", "same"),
                                         ("p1", "other", "new"), ("p1", "g", "same"),
                                         ("__rule_sentinel_system__", "__ingress_overflow__", "system")):
            queue.record_skip(platform_id=platform, group_id=group, message_id=message, reason="skip")
        self.request.query = {"all_groups": "true", "limit": "2"}
        first = (await self.plugin.list_messages())["data"]
        self.assertEqual([(row["platform_id"], row["group_id"], row["message_id"]) for row in first],
                         [("p1", "g", "same"), ("p1", "other", "new")])
        self.assertEqual(len(first[0]["events"]), 2)
        self.request.query["before_id"] = str(first[-1]["id"])
        second = (await self.plugin.list_messages())["data"]
        self.assertEqual([(row["platform_id"], row["group_id"], row["message_id"]) for row in second], [("p2", "g", "same")])
        self.assertEqual(len(second[0]["events"]), 1)
        self.request.query = {"all_groups": "true"}
        summary = (await self.plugin.summary())["data"]
        self.assertEqual(summary["total_messages"], 3)
        self.assertEqual(summary["recent_24h"]["messages"], 3)
        for query in ({"all_groups": "false"}, {"all_groups": "true", "platform_id": "p1"},
                      {"all_groups": "true", "group_id": "g"}):
            self.request.query = query
            self.assertEqual((await self.plugin.list_messages())["status_code"], 400)
            self.assertEqual((await self.plugin.summary())["status_code"], 400)
            self.assertEqual((await self.plugin.stream_events())["status_code"], 400)
        self.request.query = {"all_groups": "true"}
        self.request.username = "api_key:synthetic"
        self.assertEqual((await self.plugin.list_messages())["status_code"], 403)
        self.assertEqual((await self.plugin.summary())["status_code"], 403)
        self.assertEqual((await self.plugin.stream_events())["status_code"], 403)

    async def test_groups_union_counts_latest_activity_without_limit(self):
        rules = self.plugin.management.rules
        queue = self.plugin.management.queue
        for group in ("rules-only", "both"):
            for rule in ("r1", "r2"):
                fields = dict(platform_id="p", group_id=group, rule_id=rule, expected_version=0,
                              text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                              severity_seconds={"low": 30}, modified_by="admin")
                rules.save(**fields)
                if rule == "r1":
                    rules.save(**{**fields, "expected_version": 1})
        queue.record_skip(platform_id="p", group_id="both", message_id="m", reason="skipped")
        queue.record_skip(platform_id="other", group_id="audit-only", message_id="m", reason="skipped")
        queue.record_skip(platform_id="__rule_sentinel_system__", group_id="__ingress_overflow__", message_id="m", reason="skipped")
        with closing(sqlite3.connect(queue.path)) as db:
            db.execute("UPDATE dry_run_events SET created_at='2026-01-02T00:00:00Z' WHERE group_id='both'")
            db.execute("UPDATE dry_run_events SET created_at='2026-01-03T00:00:00Z' WHERE group_id='audit-only'")
            db.commit()
        result = (await self.plugin.list_groups())["data"]
        self.assertEqual(result, [
            {"platform_id": "other", "group_id": "audit-only", "rule_count": 0, "last_activity": "2026-01-03T00:00:00Z", "alias": "", "platform_name": ""},
            {"platform_id": "p", "group_id": "both", "rule_count": 2, "last_activity": "2026-01-02T00:00:00Z", "alias": "", "platform_name": ""},
            {"platform_id": "p", "group_id": "rules-only", "rule_count": 2, "last_activity": None, "alias": "", "platform_name": ""},
        ])
        for index in range(60):
            queue.record_skip(platform_id="p", group_id=f"g{index:02}", message_id="m", reason="skipped")
        result = (await self.plugin.list_groups())["data"]
        self.assertEqual(len(result), 63)
        self.assertTrue(any(row["group_id"] == "rules-only" for row in result))

    async def test_group_list_includes_monitoring_and_override_only_groups(self):
        self.plugin.config["monitoring_groups"] = ["p1/monitor-only"]
        self.plugin.config["group_overrides"] = {"p2/override-only": {"strategy": "priority"}}
        groups = (await self.plugin.list_groups())["data"]
        self.assertEqual({(item["platform_id"], item["group_id"]) for item in groups},
                         {("p1", "monitor-only"), ("p2", "override-only")})
        self.assertTrue(all(item["rule_count"] == 0 and item["last_activity"] is None for item in groups))

    async def test_summary_counts_latest_per_message_and_bounds(self):
        path = self.plugin.management.queue.path
        with closing(sqlite3.connect(path)) as db:
            for group, message, status, detail, timestamp in (
                ("g", "one", "started", "{}", "2020-01-01T00:00:00Z"),
                ("g", "one", "proposed", '{"model_latency_ms": 12}', None),
                ("g", "two", "no_action", '{"model_latency_ms": 20}', None),
                ("g", "old", "skipped", "{}", "2020-01-01T00:00:00Z"),
                ("other", "one", "failed", "{}", None),
            ):
                db.execute("""INSERT INTO dry_run_events(platform_id,group_id,message_id,status,detail,created_at)
                    VALUES ('p',?,?,?,?,COALESCE(?,strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
                    (group, message, status, detail, timestamp))
            db.commit()
        self.request.query = {"platform_id": "p", "group_id": "g"}
        result = (await self.plugin.summary())["data"]
        self.assertEqual(result["total_messages"], 3)
        self.assertEqual(result["status_counts"], {"proposed": 1, "no_action": 1, "skipped": 1})
        self.assertEqual(result["recent_24h"], {"messages": 2, "proposed": 1, "no_action": 1})
        self.assertEqual(result["latency_p95_ms"], 20)
        self.assertFalse(result["truncated"])
        with closing(sqlite3.connect(path)) as db:
            db.execute("""INSERT INTO dry_run_events(platform_id,group_id,message_id,status,detail)
                          VALUES ('p','g','two','started','{}')""")
            db.commit()
        latest = (await self.plugin.summary())["data"]
        self.assertEqual(latest["total_messages"], 3)
        self.assertEqual(latest["status_counts"], {"proposed": 1, "started": 1, "skipped": 1})
        self.assertEqual(latest["recent_24h"]["no_action"], 0)
        self.assertEqual(latest["latency_p95_ms"], 12)
        queue_module = sys.modules["sentinel_test_plugin.sentinel.dry_run_queue"]
        with patch.object(queue_module, "SUMMARY_LIMIT", 2):
            result = (await self.plugin.summary())["data"]
        self.assertTrue(result["truncated"])
        self.assertEqual((result["total_messages"], result["limit"]), (2, 2))
        for query in ({}, {"platform_id": "p", "group_id": ""},
                      {"platform_id": "x" * 129, "group_id": "g"}):
            self.request.query = query
            self.assertEqual((await self.plugin.summary())["status_code"], 400)

    async def test_unconfigured_model_keeps_monitoring_stopped_and_settings_editable(self):
        status = (await self.plugin.status())["data"]
        self.assertEqual(status["monitoring"], "disabled")
        self.assertEqual(status["start_error"], "model_not_configured")
        self.assertEqual(status["backend"], "llm")
        self.assertNotIn("configured_enabled", status)
        self.assertTrue(status["action_execution_enabled"])
        self.assertFalse(status["action_execution_available"])
        self.assertEqual((await self.plugin.get_settings())["data"]["llm_provider_id"], "")
        self.assertNotIn("monitoring_enabled", (await self.plugin.get_settings())["data"])
        self.raw_body = b'{"monitoring_enabled":false}'
        self.assertEqual((await self.plugin.save_settings())["status_code"], 400)
        self.assertIsNone(self.plugin.host)
        self.assertIsNone(self.plugin.intake)
        await self.plugin.capture_group_message(None)
        self.raw_body = b'{"message_retention_days": 10}'
        result = await self.plugin.save_settings()
        self.assertEqual(result["data"]["start_error"], "model_not_configured")
        self.assertEqual(result["data"]["settings"]["message_retention_days"], 10)
        self.raw_body = b'{"platform_id":"p","group_id":"g","enabled":true}'
        self.assertEqual((await self.plugin.set_group_monitoring())["data"]["start_error"], "model_not_configured")
        host = types.SimpleNamespace(intake=object(), mute_executor=object(), start=AsyncMock(), close=AsyncMock())
        with patch.object(self.module, "DryRunHost", return_value=host):
            result = await self.plugin._change_settings({"decision_backend": "llm", "llm_provider_id": "model"})
            self.assertEqual(result["monitoring"], "dry_run")
            self.assertIsNone(result["start_error"])
            self.assertTrue((await self.plugin.status())["data"]["action_execution_available"])
            host.start.assert_awaited_once()
            await self.plugin._stop_monitoring()
            host.close.assert_awaited_once()

    async def test_legacy_unselected_backend_does_not_activate_a_saved_provider(self):
        with patch.object(self.module, "DryRunHost") as host_factory:
            result = await self.plugin._change_settings({"decision_backend": "", "llm_provider_id": "old-provider"})
            self.assertEqual(result["start_error"], "model_not_configured")
            self.assertEqual((await self.plugin.status())["data"]["backend"], "none")
            await self.plugin._change_settings({"message_retention_days": 10})
            self.assertEqual(self.plugin.config["decision_backend"], "")
            self.assertIsNone(self.plugin.intake)
            host_factory.assert_not_called()

    async def test_previously_disabled_monitoring_starts_when_model_is_configured(self):
        config = {**self.plugin.config, "monitoring_enabled": False,
                  "llm_provider_id": "model", "monitoring_groups": ["p/g"],
                  "action_execution_enabled": False}
        with closing(sqlite3.connect(self.plugin._queue.path)) as db:
            db.execute("UPDATE dashboard_settings SET payload=? WHERE id=1", (json.dumps(config),))
            db.commit()
        host = types.SimpleNamespace(intake=object(), mute_executor=None, start=AsyncMock(), close=AsyncMock())
        plugin = self.module.RuleSentinel(self.context, {})
        with patch.object(self.module, "DryRunHost", return_value=host):
            await plugin.initialize()
            try:
                self.assertNotIn("monitoring_enabled", plugin.config)
                self.assertFalse(plugin.config["action_execution_enabled"])
                self.assertEqual((await plugin.status())["data"]["monitoring"], "dry_run")
                self.assertFalse((await plugin.status())["data"]["action_execution_available"])
                host.start.assert_awaited_once()
            finally:
                await plugin.terminate()

    async def test_status_reports_monitoring_start_errors_without_exception_details(self):
        self.assertEqual((await self.plugin.status())["data"]["start_error"], "model_not_configured")
        config = {"dashboard_admins": ["admin"],
                  "monitoring_groups": ["p/g"], "decision_backend": "llm", "llm_provider_id": "model"}
        await self.plugin._change_settings({key: value for key, value in config.items() if key != "dashboard_admins"})
        await self.plugin._stop_monitoring()
        for error, expected in ((self.module.LockInUseError("secret path"), "lock_in_use"),
                                (RuntimeError("secret path"), "start_failed")):
            plugin = self.module.RuleSentinel(self.context, dict(config))
            with patch.object(self.module.DryRunHost, "start", new=AsyncMock(side_effect=error)):
                await plugin.initialize()
            status = (await plugin.status())["data"]
            self.assertEqual(status["start_error"], expected)
            self.assertNotIn("secret", json.dumps(status))
            await plugin.terminate()
        await asyncio.to_thread(self.plugin.settings_store.save, {**self.plugin.config, "llm_provider_id": ""})
        plugin = self.module.RuleSentinel(self.context, {**config, "monitoring_groups": ["invalid"]})
        await plugin.initialize()
        plugin.config["monitoring_groups"] = ["invalid"]
        await plugin._start_monitoring()
        self.assertEqual((await plugin.status())["data"]["start_error"], "invalid_configuration")
        await plugin.terminate()

    async def test_running_monitoring_has_no_start_error(self):
        config = {"dashboard_admins": ["admin"],
                  "monitoring_groups": ["p/g"], "decision_backend": "llm", "llm_provider_id": "model"}
        await asyncio.to_thread(self.plugin.settings_store.save, {**self.plugin.config, **{k: v for k, v in config.items() if k != "dashboard_admins"}})
        plugin = self.module.RuleSentinel(self.context, config)
        await plugin.initialize()
        try:
            status = (await plugin.status())["data"]
            self.assertEqual(status["monitoring"], "dry_run")
            self.assertIsNone(status["start_error"])
            second = self.module.RuleSentinel(self.context, dict(config))
            await second.initialize()
            try:
                self.assertEqual((await second.status())["data"]["start_error"], "lock_in_use")
            finally:
                await second.terminate()
        finally:
            await plugin.terminate()

    async def test_settings_save_restarts_and_keeps_secret(self):
        first = types.SimpleNamespace(intake=object(), start=AsyncMock(), close=AsyncMock())
        second = types.SimpleNamespace(intake=object(), start=AsyncMock(), close=AsyncMock())
        with patch.object(self.module, "DryRunHost", side_effect=[first, second]):
            result = await self.plugin._change_settings({"decision_backend": "jev", "jev_model": "model", "jev_api_key": "secret"})
            self.assertEqual(result["monitoring"], "dry_run")
            self.assertNotIn("jev_api_key", result["settings"])
            await self.plugin._change_settings({"model_concurrency": 3, "jev_api_key": ""})
            first.close.assert_awaited_once()
            second.start.assert_awaited_once()
            self.assertEqual(self.plugin.config["jev_api_key"], "secret")
            with self.assertRaises(ValueError):
                await self.plugin._change_settings({"model_concurrency": 0})
            await self.plugin._stop_monitoring()
            second.close.assert_awaited_once()
            self.assertIsNone(self.plugin.intake)

    async def test_reason_only_setting_keeps_monitor_running(self):
        first = types.SimpleNamespace(intake=object(), start=AsyncMock(), close=AsyncMock())
        second = types.SimpleNamespace(intake=object(), start=AsyncMock(), close=AsyncMock())
        with patch.object(self.module, "DryRunHost", side_effect=[first, second]):
            await self.plugin._change_settings({"decision_backend": "llm", "llm_provider_id": "model"})
            fields = {key: value for key, value in self.plugin.config.items()
                      if key not in ("group_overrides", "monitoring_groups", "jev_api_key")}
            result = await self.plugin._change_settings({**fields, "llm_no_action_reason_enabled": False, "jev_api_key": ""})
            self.assertEqual(result["monitoring"], "dry_run")
            self.assertIs(self.plugin.host, first)
            self.assertIs(self.plugin.intake, first.intake)
            first.close.assert_not_awaited()
            self.assertFalse(self.plugin.config["llm_no_action_reason_enabled"])
            self.assertFalse((await asyncio.to_thread(self.plugin.settings_store.load_or_migrate, {}))["llm_no_action_reason_enabled"])
            await self.plugin._change_settings({"llm_no_action_reason_enabled": True})
            self.assertIs(self.plugin.host, first)
            first.close.assert_not_awaited()
            await self.plugin._change_settings({"llm_no_action_reason_enabled": False, "model_concurrency": 3})
            first.close.assert_awaited_once()
            second.start.assert_awaited_once()
            await self.plugin._stop_monitoring()
            second.close.assert_awaited_once()

    async def test_reason_setting_change_during_model_call_keeps_host_and_current_decision(self):
        await self.plugin._change_settings({"monitoring_groups": ["p/g"],
                                            "decision_backend": "llm", "llm_provider_id": "model"})
        started, release = asyncio.Event(), asyncio.Event()
        drain = None
        try:
            host, intake = self.plugin.host, self.plugin.intake
            await asyncio.to_thread(self.plugin._rules.save, platform_id="p", group_id="g", rule_id="r",
                expected_version=0, text="No spam", enabled=True, modified_by="admin", allowed_actions=["mute"],
                duration_mode="strict", severity_seconds={"low": 60})
            await asyncio.to_thread(self.plugin._queue.enqueue, platform_id="p", group_id="g", message_id="first",
                capacity=10, payload={"text": "first message", "sender_id": "sender"})
            router = host.runtime.worker.decide.decide
            make_decider = router.make_decider
            async def invoke(**kwargs):
                if kwargs["message"] == "first message":
                    started.set()
                    await release.wait()
                return types.SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=[], tools_call_args=[],
                                             completion_text="NO_ACTION\nNo violation.")
            def make(*route):
                decider = make_decider(*route)
                for enabled in (False, True):
                    decider.for_setting(enabled).invoke = invoke
                return decider
            router.make_decider = make
            drain = asyncio.create_task(host.runtime.worker.drain_group("p", "g"))
            await asyncio.wait_for(started.wait(), timeout=10)
            await asyncio.wait_for(self.plugin._change_settings({"llm_no_action_reason_enabled": False}), timeout=10)
            self.assertIs(self.plugin.host, host)
            self.assertIs(self.plugin.intake, intake)
            self.assertFalse(drain.done())
            await asyncio.to_thread(self.plugin._queue.enqueue, platform_id="p", group_id="g", message_id="second",
                capacity=10, payload={"text": "second message", "sender_id": "sender"})
            release.set()
            self.assertEqual(await asyncio.wait_for(drain, timeout=10), 2)
            events = [event for event in self.plugin._queue.list_events(platform_id="p", group_id="g") if event["status"] != "started"]
            self.assertEqual([event["status"] for event in events], ["no_action", "no_action"])
            first, second = [json.loads(event["detail"]) for event in events]
            self.assertTrue(first["no_action_reason_enabled"])
            self.assertEqual(first["reason"], "No violation.")
            self.assertFalse(second["no_action_reason_enabled"])
            self.assertNotIn("reason", second)
        finally:
            release.set()
            if drain is not None and not drain.done():
                await asyncio.gather(drain, return_exceptions=True)
            await self.plugin._stop_monitoring()

    async def test_invalid_or_oversized_body_is_rejected(self):
        for body in (b"not json", b"x" * (self.module.MAX_BODY_BYTES + 1), b'{"x":1,"x":2}',
                     b'{"x":{"nested":1,"nested":2}}', b'{"x":NaN}', b'{"x":' + b'[' * 2000 + b']' * 2000 + b'}'):
            with self.subTest(body=body[:40]):
                self.raw_body = body
                self.assertEqual((await self.plugin.preview_rule())["status_code"], 400)

    async def test_declared_oversized_or_invalid_length_is_rejected_before_read(self):
        async def forbidden_body():
            self.fail("invalid declared body length was read")
        self.request.body = forbidden_body
        for length in (str(self.module.MAX_BODY_BYTES + 1), "-1", "NaN", "1" * 11):
            with self.subTest(length=length):
                self.request.headers = {"content-type": "application/json", "content-length": length}
                self.assertEqual((await self.plugin.preview_rule())["status_code"], 400)
        self.request.headers = types.SimpleNamespace(getlist=lambda key: ["application/json"]
                                                     if key == "content-type" else ["1", "1"])
        self.assertEqual((await self.plugin.preview_rule())["status_code"], 400)

    async def test_declared_body_length_must_match_actual_body(self):
        self.raw_body = b"{}"
        self.request.headers = {"content-type": "application/json", "content-length": "1"}
        self.assertEqual((await self.plugin.preview_rule())["status_code"], 400)

    async def test_json_depth_check_ignores_quoted_and_escaped_brackets(self):
        payload = {"text": '"' + "[" * 200 + '\\"' + "]" * 200}
        self.raw_body = json.dumps(payload).encode()
        self.request.headers = {"content-type": "application/json", "content-length": str(len(self.raw_body))}
        self.assertEqual(await self.plugin._payload(), payload)

    async def test_form_or_ambiguous_content_type_is_rejected_before_read(self):
        async def forbidden_body():
            self.fail("unsupported request body was read")
        self.request.body = forbidden_body
        for headers in ({}, {"content-type": "text/plain"},
                        {"content-type": "application/x-www-form-urlencoded"},
                        {"content-type": "multipart/form-data; boundary=x"},
                        types.SimpleNamespace(getlist=lambda key: ["application/json", "text/plain"]
                                              if key == "content-type" else [])):
            with self.subTest(headers=headers):
                self.request.headers = headers
                self.assertEqual((await self.plugin.preview_rule())["status_code"], 400)
                self.assertEqual((await self.plugin.confirm_rule())["status_code"], 400)

    async def test_json_content_type_with_charset_is_accepted(self):
        self.raw_body = b'{}'
        self.request.headers = {"content-type": "Application/JSON; charset=utf-8"}
        self.assertEqual(await self.plugin._payload(), {})

    async def test_messages_group_latest_event_timelines_and_pagination(self):
        path = self.plugin.management.queue.path
        with closing(sqlite3.connect(path)) as db:
            for group, message, status, detail, timestamp in (
                ("g", "first", "started", "start", "2026-01-01T00:00:01Z"),
                ("g", "waiting", "started", "pending", "2026-01-01T00:00:02Z"),
                ("g", "first", "proposed", '{"reason":"x"}', "2026-01-01T00:00:03Z"),
                ("other", "first", "failed", "other", "2026-01-01T00:00:04Z"),
                ("g", "retry", "started", "start", "2026-01-01T00:00:05Z"),
                ("g", "retry", "failed", "failed", "2026-01-01T00:00:06Z"),
                ("g", "retry", "started", "retry", "2026-01-01T00:00:07Z"),
                ("g", "retry", "no_action", "done", "2026-01-01T00:00:08Z"),
            ):
                db.execute("""INSERT INTO dry_run_events(platform_id,group_id,message_id,status,detail,created_at)
                    VALUES ('p',?,?,?,?,?)""", (group, message, status, detail, timestamp))
            db.commit()
        self.request.query = {"platform_id": "p", "group_id": "g", "limit": "2"}
        first = (await self.plugin.list_messages())["data"]
        self.assertEqual([(row["message_id"], row["status"]) for row in first],
                         [("retry", "no_action"), ("first", "proposed")])
        self.assertEqual(first[1]["first_created_at"], "2026-01-01T00:00:01Z")
        self.assertEqual(first[1]["created_at"], "2026-01-01T00:00:03Z")
        self.assertEqual(first[1]["detail"], '{"reason":"x"}')
        self.assertEqual([event["status"] for event in first[1]["events"]], ["started", "proposed"])
        self.assertTrue(all(set(event) == {"id", "status", "created_at"} for event in first[1]["events"]))
        self.assertEqual([event["status"] for event in first[0]["events"]], ["started", "failed", "started", "no_action"])
        with closing(sqlite3.connect(path)) as db:
            db.execute("INSERT INTO dry_run_events(platform_id,group_id,message_id,status,detail) VALUES ('p','g','new','started','start')")
            db.commit()
        self.request.query["before_id"] = str(first[-1]["id"])
        second = (await self.plugin.list_messages())["data"]
        self.assertEqual([(row["message_id"], row["status"]) for row in second], [("waiting", "started")])
        self.request.query["before_id"] = str(second[-1]["id"])
        self.assertEqual((await self.plugin.list_messages())["data"], [])
        for value in ("0", "-1", "invalid", "9223372036854775808"):
            self.request.query["before_id"] = value
            self.assertEqual((await self.plugin.list_messages())["status_code"], 400)
        self.request.query.pop("before_id")
        self.request.query["after_id"] = "0"
        self.assertEqual((await self.plugin.list_messages())["status_code"], 400)
        self.request.query.pop("after_id")
        self.assertEqual((await self.plugin.list_messages())["data"][0]["message_id"], "new")
        for value in ("0", "101", "invalid"):
            self.request.query["limit"] = value
            self.assertEqual((await self.plugin.list_messages())["status_code"], 400)

    async def test_terminate_unhooks_only_own_routes(self):
        self.context.routes.append(("/other/x", lambda: None, ["GET"], "x"))
        cleanup = self.plugin._retention_task
        await self.plugin.terminate()
        self.assertTrue(cleanup.cancelled())
        self.assertIsNone(self.plugin._retention_task)
        self.assertEqual([route[0] for route in self.context.routes], ["/other/x"])
        self.assertEqual((await self.plugin.list_rules())["status_code"], 503)

    async def test_message_content_route_requires_dashboard_identity_and_scope(self):
        await asyncio.to_thread(self.plugin._queue.enqueue, platform_id="p", group_id="g", message_id="m",
                                capacity=10, payload={"text": "original"})
        self.request.query = {"platform_id": "p", "group_id": "g", "message_id": "m"}
        self.assertEqual((await self.plugin.message_content())["data"], {"content": "original"})
        self.request.query["group_id"] = "other"
        self.assertEqual((await self.plugin.message_content())["data"], {"content": None})
        for username in ("", "api_key:synthetic"):
            self.request.username = username
            self.assertEqual((await self.plugin.message_content())["status_code"], 403)
        self.request.username = "admin"
        self.request.query.pop("message_id")
        self.assertEqual((await self.plugin.message_content())["status_code"], 400)

    async def test_retention_cleanup_runs_on_start_without_a_model(self):
        await asyncio.to_thread(self.plugin._queue.enqueue, platform_id="p", group_id="g", message_id="old",
                                capacity=10, payload={"text": "expired"})
        with closing(sqlite3.connect(self.plugin._queue.path)) as db:
            db.execute("UPDATE message_contents SET stored_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-31 days')")
            db.commit()
        other = self.module.RuleSentinel(self.context, {})
        await other.initialize()
        try:
            with closing(sqlite3.connect(self.plugin._queue.path)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM message_contents").fetchone()[0], 0)
            self.assertIsNotNone(other._retention_task)
        finally:
            await other.terminate()

    async def test_retention_cleanup_repeats_until_cancelled(self):
        with patch.object(self.module.asyncio, "sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError()])), \
                patch.object(self.module, "run_sync_safely", new=AsyncMock()) as purge:
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin._clean_message_contents()
            purge.assert_awaited_once_with(self.plugin._queue.purge_message_contents)

    async def test_snapshot_prefilter_does_not_extract_inactive_group_text(self):
        policy = types.SimpleNamespace(enabled=True, mode="blocklist", groups=set())
        admission = types.SimpleNamespace(policy=policy, settings_match=None)
        offered = []
        self.plugin.intake = types.SimpleNamespace(runtime=types.SimpleNamespace(admission=admission),
                                                   offer=offered.append)
        event = types.SimpleNamespace(message_obj=types.SimpleNamespace(group_id="g"),
                                      get_platform_id=lambda: "p")
        with patch.object(self.module, "extract") as extract:
            await self.plugin.capture_group_message(event)
            extract.assert_not_called()
            self.assertEqual(offered, [])
            fields = dict(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                          text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                          severity_seconds={"low": 30})
            self.plugin.management.confirm("admin", self.plugin.management.preview("admin", fields))
            extract.return_value = types.SimpleNamespace(message=None, status="unsupported_platform")
            await self.plugin.capture_group_message(event)
            extract.assert_called_once()
            self.plugin.management.confirm("admin", self.plugin.management.preview(
                "admin", {**fields, "expected_version": 1, "enabled": False}))
            extract.reset_mock()
            await self.plugin.capture_group_message(event)
            extract.assert_not_called()
        self.plugin.intake = None

    async def test_commands_never_reach_intake_but_wake_text_is_copied(self):
        policy = types.SimpleNamespace(enabled=True, mode="blocklist", groups=set())
        admission = types.SimpleNamespace(policy=policy, settings_match=None)
        offered = []
        self.plugin.intake = types.SimpleNamespace(runtime=types.SimpleNamespace(admission=admission), offer=offered.append)
        self.plugin.group_index = types.SimpleNamespace(is_enabled=lambda *_: True)
        self.context.get_config = lambda *, umo: {"wake_prefix": ["/", "!"]}
        plain = sys.modules["astrbot.api.message_components"].Plain()
        raw = types.SimpleNamespace(group_openid="g", raw_data={"author": {"member_openid": "sender", "bot": False}, "message_type": 0})
        obj = types.SimpleNamespace(raw_message=raw, group_id="g", message_id="message", message=[plain], message_str="/ 加微信拉群")
        event = types.SimpleNamespace(message_obj=obj, get_platform_name=lambda: "qq_official", get_platform_id=lambda: "p",
                                      get_sender_id=lambda: "sender", unified_msg_origin="p:GroupMessage:g")
        try:
            with patch.dict(sys.modules, command_modules()):
                for kind in (CommandFilter, CommandGroupFilter, RegexFilter):
                    handler = types.SimpleNamespace(handler=lambda: None, event_filters=[kind()])
                    event.get_extra = lambda _, item=handler: [item]
                    await self.plugin.capture_group_message(event)
                    self.assertEqual(offered, [])
                    self.assertEqual(self.plugin.management.list_audit("admin", "p", "g"), [])
                event.get_extra = lambda _: []
                await self.plugin.capture_group_message(event)
                self.assertEqual(offered[-1].text, "加微信拉群")
                self.assertEqual(offered[-1].original_text, "/ 加微信拉群")
                self.assertEqual(obj.message_str, "/ 加微信拉群")
            event.get_extra = lambda _: [types.SimpleNamespace(event_filters=[CommandFilter()])]
            with patch.dict(sys.modules, {name: None for name in command_modules()}):
                await self.plugin.capture_group_message(event)
                self.assertEqual(offered[-1].text, "加微信拉群")
        finally:
            self.plugin.intake = None

    async def test_monitoring_listener_produces_a_dry_run_without_touching_the_event(self):
        config = {"dashboard_admins": ["admin"],
                  "monitoring_groups": ["p/g"], "decision_backend": "llm", "llm_provider_id": "model"}
        await asyncio.to_thread(self.plugin.settings_store.save, {**self.plugin.config, **{k: v for k, v in config.items() if k != "dashboard_admins"}})
        plugin = self.module.RuleSentinel(self.context, config)
        await plugin.initialize()
        management = plugin.management
        try:
            self.assertIsNotNone(plugin.intake)
            self.assertIsNotNone(plugin.host)
            fields = dict(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                          text="No spam", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                          severity_seconds={"low": 30})
            plugin.management.confirm("admin", plugin.management.preview("admin", fields))
            seen_messages = []
            self.context.get_config = lambda *, umo: {"wake_prefix": ["/"]}
            async def invoke(**kwargs):
                seen_messages.append(kwargs["message"])
                return types.SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["propose_action"],
                    tools_call_args=[{"violated": True, "rule_id": "r", "severity": "low", "reason": "spam"}])
            router = plugin.host.runtime.worker.decide.decide
            make_decider = router.make_decider
            def fake_decider(backend, model, strategy, reason_language):
                decider = make_decider(backend, model, strategy, reason_language)
                decider.current().invoke = invoke
                return decider
            router.make_decider = fake_decider
            plain = sys.modules["astrbot.api.message_components"].Plain()
            raw = types.SimpleNamespace(group_openid="g", raw_data={"author": {"member_openid": "sender", "bot": False}, "message_type": 0})
            obj = types.SimpleNamespace(raw_message=raw, group_id="g", message_id="message",
                                        message=[plain], message_str="/ private message")
            def forbidden(*args, **kwargs):
                self.fail("the passive listener must not reply or stop the event")
            event = types.SimpleNamespace(message_obj=obj, get_platform_name=lambda: "qq_official",
                get_platform_id=lambda: "p", get_sender_id=lambda: "sender", is_wake=False,
                unified_msg_origin="p:GroupMessage:g", stop_event=forbidden, send=forbidden,
                set_result=forbidden)
            await plugin.capture_group_message(event)
            self.assertFalse(event.is_wake)
            await asyncio.wait_for(plugin.intake.inbox.join(), timeout=2)
            await asyncio.wait_for(asyncio.gather(*list(plugin.host.runtime._tasks.values())), timeout=2)
            self.assertEqual(seen_messages, ["private message"])
            self.assertEqual(management.message_content("admin", "p", "g", "message"), {"content": "/ private message"})
            self.assertEqual(obj.message_str, "/ private message")
            obj.message_id, obj.message_str = "empty", "/  "
            await plugin.capture_group_message(event)
            await asyncio.wait_for(plugin.intake.inbox.join(), timeout=2)
        finally:
            await plugin.terminate()
        events = management.list_audit("admin", "p", "g")
        self.assertEqual([event["status"] for event in events], ["started", "proposed", "skipped"])
        self.assertEqual(events[-1]["detail"], "empty_text")
        self.assertNotIn("private message", events[1]["detail"])
