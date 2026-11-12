"""Probe AstrBot's actual scheduler and safety stage with synthetic messages.

No stages are registered or modified in the running instance. This probe uses
an uninitialized scheduler with an explicit isolated stage list.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix="sentinel-safety-probe-") as root:
        os.environ["ASTRBOT_ROOT"] = root
        from astrbot.core.pipeline.content_safety_check.stage import ContentSafetyCheckStage
        from astrbot.core.pipeline.scheduler import PipelineScheduler
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.platform.platform_metadata import PlatformMetadata
        from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            PatchedGroupMessage, QQOfficialPlatformAdapter,
        )

        async def event():
            raw = PatchedGroupMessage(None, "event", {"id": "m", "group_openid": "g", "content": "example",
                "author": {"member_openid": "sender", "bot": False}, "attachments": [], "mentions": [],
                "message_type": 0})
            message = await QQOfficialPlatformAdapter._parse_from_qqofficial(raw, MessageType.GROUP_MESSAGE)
            message.group_id = message.session_id = "g"
            return QQOfficialMessageEvent(message.message_str, message,
                PlatformMetadata(name="qq_official", description="test", id="p"), "g", None)

        async def run_case(*, allowed, waking):
            safety = ContentSafetyCheckStage()
            safety.strategy_selector = SimpleNamespace(check=lambda text: (allowed, "synthetic"))
            seen = []
            class LaterStage:
                async def process(self, message):
                    seen.append(message.is_stopped())
            scheduler = PipelineScheduler.__new__(PipelineScheduler)
            scheduler.stages = [safety, LaterStage()]
            message = await event()
            message.is_at_or_wake_command = waking
            await scheduler._process_stages(message)
            return seen, message.is_stopped()

        async def probe():
            normal, stopped = await run_case(allowed=True, waking=False)
            assert normal == [False] and not stopped
            blocked, stopped = await run_case(allowed=False, waking=False)
            assert blocked == [True] and stopped
            blocked_waking, stopped = await run_case(allowed=False, waking=True)
            assert blocked_waking == [False, True] and stopped

        asyncio.run(probe())
    print("Synthetic safety scheduler behavior reproduced; no running pipeline or network used.")


if __name__ == "__main__":
    main()
