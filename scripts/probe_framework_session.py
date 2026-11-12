"""Compare the passive gate with actual AstrBot stages using synthetic data.

Session storage and configuration manager are fakes; this is not a running
AstrBot instance or proof that a passive listener can be enabled safely.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix="sentinel-session-probe-") as root:
        os.environ["ASTRBOT_ROOT"] = root
        from astrbot.core.pipeline.session_status_check.stage import SessionStatusCheckStage
        from astrbot.core.pipeline.stage_order import STAGES_ORDER
        from astrbot.core.pipeline.whitelist_check.stage import WhitelistCheckStage
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.platform.platform_metadata import PlatformMetadata
        from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            PatchedGroupMessage, QQOfficialPlatformAdapter,
        )
        from astrbot.core.star.context import Context
        from astrbot.core.star import session_llm_manager, session_plugin_manager
        from sentinel.framework_session_gate import build_astrbot_session_gate

        origin, plugin_name = "p:GroupMessage:g", "astrbot_plugin_rule_sentinel"

        class Store:
            def __init__(self):
                self.session_enabled = True
                self.plugin_disabled = False
                self.fail = False
            async def get_async(self, *, scope, scope_id, key, default):
                assert scope == "umo" and scope_id == origin
                if self.fail:
                    raise RuntimeError("synthetic storage failure")
                if key == "session_service_config":
                    return {"session_enabled": self.session_enabled}
                if key == "session_plugin_config":
                    return {origin: {"disabled_plugins": [plugin_name] if self.plugin_disabled else []}}
                raise AssertionError("unexpected setting")

        async def make_event():
            raw = PatchedGroupMessage(None, "event", {"id": "m", "group_openid": "g", "content": "hello",
                "author": {"member_openid": "sender", "bot": False}, "attachments": [], "mentions": [],
                "message_type": 0})
            message = await QQOfficialPlatformAdapter._parse_from_qqofficial(raw, MessageType.GROUP_MESSAGE)
            message.group_id = message.session_id = "g"
            return QQOfficialMessageEvent(message.message_str, message,
                PlatformMetadata(name="qq_official", description="test", id="p"), "g", None)

        async def probe():
            assert STAGES_ORDER.index("WakingCheckStage") < STAGES_ORDER.index("WhitelistCheckStage")
            assert STAGES_ORDER.index("WhitelistCheckStage") < STAGES_ORDER.index("SessionStatusCheckStage")
            assert STAGES_ORDER.index("SessionStatusCheckStage") < STAGES_ORDER.index("RateLimitStage")
            assert STAGES_ORDER.index("RateLimitStage") < STAGES_ORDER.index("ContentSafetyCheckStage")
            store = Store()
            config = {"plugin_set": ["*"], "platform_settings": {"enable_id_white_list": True,
                "id_whitelist": ["g"], "wl_ignore_admin_on_group": False,
                "wl_ignore_admin_on_friend": False, "id_whitelist_log": False}}
            class Manager:
                def get_conf(self, umo):
                    assert umo == origin
                    return config
            context = SimpleNamespace(astrbot_config_mgr=Manager())
            context.get_config = lambda *, umo: Context.get_config(context, umo)
            gate = build_astrbot_session_gate(context, plugin_name)
            async def existing_conversation(umo):
                return "existing"
            conversation = SimpleNamespace(get_curr_conversation_id=existing_conversation)
            stage_context = SimpleNamespace(astrbot_config=config,
                plugin_manager=SimpleNamespace(context=SimpleNamespace(conversation_manager=conversation)))
            with patch.object(session_llm_manager, "sp", store), patch.object(session_plugin_manager, "sp", store):
                for whitelist, session_enabled, plugin_disabled, expected in (
                    (["g"], True, False, True), ([origin], True, False, True),
                    ([], True, False, True), (["other"], True, False, False),
                    (["g"], False, False, False), (["g"], True, True, False)):
                    config["platform_settings"]["id_whitelist"] = whitelist
                    store.session_enabled, store.plugin_disabled = session_enabled, plugin_disabled
                    event = await make_event()
                    whitelist_stage, session_stage = WhitelistCheckStage(), SessionStatusCheckStage()
                    await whitelist_stage.initialize(stage_context)
                    await session_stage.initialize(stage_context)
                    await whitelist_stage.process(event)
                    allowed_whitelist = not event.is_stopped()
                    if allowed_whitelist:
                        await session_stage.process(event)
                    assert await gate(SimpleNamespace(platform_id="p", group_id="g", session_origin=origin)) is expected
                    assert allowed_whitelist == (not whitelist or "g" in whitelist or origin in whitelist)
                    if allowed_whitelist:
                        assert event.is_stopped() is (not session_enabled)

                config["platform_settings"]["id_whitelist"] = ["other"]
                config["platform_settings"]["wl_ignore_admin_on_group"] = True
                store.session_enabled, store.plugin_disabled = True, False
                event = await make_event()
                event.role = "admin"
                whitelist_stage = WhitelistCheckStage()
                await whitelist_stage.initialize(stage_context)
                await whitelist_stage.process(event)
                assert not event.is_stopped()
                assert not await gate(SimpleNamespace(platform_id="p", group_id="g", session_origin=origin))
                config["platform_settings"]["id_whitelist"] = ["g"]
                store.fail = True
                assert not await gate(SimpleNamespace(platform_id="p", group_id="g", session_origin=origin))
                store.fail = False
                assert not await gate(SimpleNamespace(platform_id="p", group_id="different", session_origin=origin))
                config["plugin_set"] = ["some_other_plugin"]
                assert not await gate(SimpleNamespace(platform_id="p", group_id="g", session_origin=origin))

        asyncio.run(probe())
    print("Synthetic AstrBot session gate comparison passed; storage and configuration were mocked.")


if __name__ == "__main__":
    main()
