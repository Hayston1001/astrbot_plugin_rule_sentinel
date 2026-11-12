"""Check the passive group listener against the real AstrBot 4.29.3 stages.

Registers a handler shaped exactly like ``main.capture_group_message`` (a single
``EventMessageType.GROUP_MESSAGE`` filter) and drives synthetic QQ group events
through the real waking, content-safety and star-request stages. It shows that
an ordinary, non-waking group message reaches the handler, and that a message
the content-safety stage blocked never does.

The handler registry entry, event and configuration are synthetic. No adapter,
server, real session store or model is started.
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
    with tempfile.TemporaryDirectory(prefix="sentinel-listener-probe-") as root:
        os.environ["ASTRBOT_ROOT"] = root
        from astrbot.core.pipeline.waking_check import stage as waking
        from astrbot.core.pipeline.content_safety_check.stage import ContentSafetyCheckStage
        from astrbot.core.pipeline.process_stage.method.star_request import StarRequestSubStage
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.platform.platform_metadata import PlatformMetadata
        from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            PatchedGroupMessage, QQOfficialPlatformAdapter,
        )
        from astrbot.core.star.filter.event_message_type import EventMessageType, EventMessageTypeFilter
        from astrbot.core.star.star import star_map
        from astrbot.core.star.star_handler import EventType, StarHandlerMetadata, star_handlers_registry

        plugin_path = "probe.main"
        calls = []

        async def capture_group_message(event):
            calls.append(event.get_message_str())

        handler = StarHandlerMetadata(
            event_type=EventType.AdapterMessageEvent,
            handler_full_name="probe.main_capture_group_message",
            handler_name="capture_group_message",
            handler_module_path=plugin_path,
            handler=capture_group_message,
            event_filters=[EventMessageTypeFilter(EventMessageType.GROUP_MESSAGE)],
            extras_configs={"priority": 10},
        )

        async def make_event(text):
            raw = PatchedGroupMessage(None, "event", {"id": "m", "group_openid": "g", "content": text,
                "author": {"member_openid": "sender", "bot": False}, "attachments": [], "mentions": [],
                "message_type": 0})
            message = await QQOfficialPlatformAdapter._parse_from_qqofficial(raw, MessageType.GROUP_MESSAGE)
            message.group_id = message.session_id = "g"
            return QQOfficialMessageEvent(message.message_str, message,
                PlatformMetadata(name="qq_official", description="test", id="p"), "g", None)

        async def identity_filter(event, handlers):
            return handlers

        async def run(text):
            calls.clear()
            check = waking.WakingCheckStage()
            check.ctx = SimpleNamespace(astrbot_config={"platform_settings": {}, "admins_id": [],
                                                       "wake_prefix": ["!"]})
            check.no_permission_reply = True
            check.friend_message_needs_wake_prefix = False
            check.ignore_bot_self_message = False
            check.ignore_at_all = False
            check.disable_builtin_commands = False
            check._umo_auto_name_recorder = SimpleNamespace(schedule=lambda event: None)
            safety = ContentSafetyCheckStage()
            safety.strategy_selector = SimpleNamespace(check=lambda content: (
                (False, "blocked") if "blocked" in content else (True, "")))
            star = StarRequestSubStage()
            await star.initialize(SimpleNamespace(
                astrbot_config={"provider_settings": {"prompt_prefix": "", "identifier": False}}))
            event = await make_event(text)
            with patch.object(waking.SessionPluginManager, "filter_handlers_by_session", identity_filter):
                await check.process(event)
                activated = len(event.get_extra("activated_handlers", []))
                if not event.is_stopped():
                    async for _ in safety.process(event):
                        pass
                if not event.is_stopped():
                    async for _ in star.process(event):
                        pass
            return event, activated, list(calls)

        async def probe():
            star_map[plugin_path] = SimpleNamespace(name="probe", display_name="Probe", activated=True)
            star_handlers_registry.append(handler)
            try:
                event, activated, seen = await run("ordinary chat")
                assert activated == 1, f"handler was not activated: {activated}"
                assert not event.is_stopped(), "an ordinary group message must not be stopped"
                assert event.is_at_or_wake_command is False, "the listener must not mark a command wake"
                assert seen == ["ordinary chat"], seen
                assert event.get_result() is None, "the listener must not produce a reply"

                event, activated, seen = await run("blocked text")
                assert activated == 1, activated
                assert event.is_stopped(), "the content-safety stage must stop a blocked message"
                assert seen == [], f"a blocked message reached the listener: {seen}"
                assert event.is_at_or_wake_command is False
            finally:
                star_handlers_registry.remove(handler)
                star_map.pop(plugin_path, None)

        asyncio.run(probe())
    print("Passive listener probe passed; registry, event and configuration were synthetic.")


if __name__ == "__main__":
    main()
