"""Compare an unregistered probe against AstrBot's actual waking stage offline.

The handler registry and session filter are replaced only in this subprocess.
No adapter, server, persistent session manager or model is started.
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
    with tempfile.TemporaryDirectory(prefix="sentinel-waking-probe-") as root:
        os.environ["ASTRBOT_ROOT"] = root
        from astrbot.core.pipeline.waking_check import stage as waking
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.platform.platform_metadata import PlatformMetadata
        from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            PatchedGroupMessage, QQOfficialPlatformAdapter,
        )
        from astrbot.core.star.filter.custom_filter import CustomFilter
        from astrbot.api.message_components import At, Plain
        from sentinel.filter_probe import make_filter
        from sentinel.qq_official_snapshot import extract

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

        async def run_case(text, *, probe, other_handler=False, observer_fails=False, waiter=False):
            seen, failures, scheduled = [], [], []
            def observe(event):
                snapshot = extract(event, is_management_command=lambda text: False, text_segment_types=(Plain, At))
                assert snapshot.status == "ok"
                seen.append(snapshot.message.text)
                if observer_fails:
                    raise ValueError("synthetic failure")
            binding = SimpleNamespace(observe=observe, on_error=lambda: failures.append(True))
            handlers = []
            if other_handler:
                handlers.append(SimpleNamespace(handler_module_path="other.main", handler_full_name="other",
                                                event_filters=[SimpleNamespace(filter=lambda event, cfg: True)]))
            if probe:
                handlers.append(SimpleNamespace(handler_module_path="probe.main", handler_full_name="probe",
                                                event_filters=[make_filter(CustomFilter, binding)()]))
            check = waking.WakingCheckStage()
            check.ctx = SimpleNamespace(astrbot_config={"platform_settings": {}, "admins_id": [],
                                                       "wake_prefix": ["!"]})
            check.no_permission_reply = True
            check.friend_message_needs_wake_prefix = False
            check.ignore_bot_self_message = False
            check.ignore_at_all = False
            check.disable_builtin_commands = False
            check._umo_auto_name_recorder = SimpleNamespace(schedule=lambda event: scheduled.append(True))
            event = await make_event(text)
            future = asyncio.get_running_loop().create_future()
            filters = [SimpleNamespace(filter=lambda event: "session")] if waiter else []
            sessions = {"session": SimpleNamespace(session_controller=SimpleNamespace(future=future))} if waiter else {}
            with patch.object(waking.star_handlers_registry, "get_handlers_by_event_type", return_value=handlers), \
                    patch.object(waking.SessionPluginManager, "filter_handlers_by_session", identity_filter), \
                    patch.object(waking, "FILTERS", filters), patch.object(waking, "USER_SESSIONS", sessions):
                await check.process(event)
            state = (event.is_wake, event.is_at_or_wake_command, event.is_stopped(), event.message_str,
                     len(event.get_extra("activated_handlers")), len(scheduled), future.done())
            return state, seen, failures

        async def probe():
            for text, other, waiter in (("ordinary", False, False), ("!command", False, False),
                                        ("ordinary", True, False), ("ordinary", False, True)):
                baseline, _, _ = await run_case(text, probe=False, other_handler=other, waiter=waiter)
                observed, seen, failures = await run_case(text, probe=True, other_handler=other, waiter=waiter)
                assert observed == baseline and seen == [text] and failures == []
                failed, seen, failures = await run_case(text, probe=True, other_handler=other,
                                                        observer_fails=True, waiter=waiter)
                assert failed == baseline and seen == [text] and failures == [True]

        asyncio.run(probe())
    print("Synthetic waking-stage comparison passed; no real registry or platform started.")


if __name__ == "__main__":
    main()
