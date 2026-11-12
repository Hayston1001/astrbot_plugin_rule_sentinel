import unittest
from types import SimpleNamespace

from sentinel.framework_session_gate import FrameworkSessionGate


class FrameworkSessionGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = {"platform_settings": {"enable_id_white_list": True, "id_whitelist": ["g"]}}
        self.session_enabled = True
        self.plugin_enabled = True
        async def session(origin):
            return self.session_enabled
        async def plugin(origin, name):
            self.assertEqual(name, "rule-sentinel")
            return self.plugin_enabled
        self.gate = FrameworkSessionGate(get_config=lambda origin: self.settings,
            is_session_enabled=session, is_plugin_enabled=plugin, plugin_name="rule-sentinel")
        self.message = SimpleNamespace(session_origin="p:GroupMessage:g", group_id="g", platform_id="p")

    async def test_allow_only_when_framework_checks_pass(self):
        self.assertTrue(await self.gate(self.message))
        self.plugin_enabled = False
        self.assertFalse(await self.gate(self.message))
        self.plugin_enabled, self.session_enabled = True, False
        self.assertFalse(await self.gate(self.message))
        self.session_enabled = True
        self.settings["platform_settings"]["id_whitelist"] = ["other"]
        self.assertFalse(await self.gate(self.message))
        self.settings["platform_settings"]["id_whitelist"] = ["g"]
        self.settings["plugin_set"] = ["another-plugin"]
        self.assertFalse(await self.gate(self.message))

    async def test_missing_identity_invalid_config_and_exceptions_deny(self):
        self.assertFalse(await self.gate(SimpleNamespace(session_origin=None, group_id="g", platform_id="p")))
        self.assertFalse(await self.gate(SimpleNamespace(session_origin="other:GroupMessage:g", group_id="g", platform_id="p")))
        self.assertFalse(await self.gate(SimpleNamespace(session_origin="p:FriendMessage:g", group_id="g", platform_id="p")))
        self.settings = {}
        self.assertFalse(await self.gate(self.message))
        self.settings = {"platform_settings": {"enable_id_white_list": True, "id_whitelist": ["g"]}}
        async def fail(origin):
            raise RuntimeError("sensitive error")
        self.gate.is_session_enabled = fail
        self.assertFalse(await self.gate(self.message))

    async def test_session_group_must_match_message_group(self):
        self.settings["platform_settings"]["id_whitelist"] = ["g", "other", "p:GroupMessage:other"]
        for group, origin in (("g", "p:GroupMessage:other"),
                              ("other", "p:GroupMessage:g"),
                              (None, "p:GroupMessage:g"),
                              ("", "p:GroupMessage:g")):
            with self.subTest(group=group, origin=origin):
                self.assertFalse(await self.gate(SimpleNamespace(
                    session_origin=origin, group_id=group, platform_id="p")))
