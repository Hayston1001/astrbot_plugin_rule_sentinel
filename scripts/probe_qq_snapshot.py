"""Offline QQ payload probe using the installed AstrBot adapter classes.

Synthetic examples only: this does not connect to QQ or establish which fields
real platform events provide. Keep monitoring disabled until actual validation.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix="sentinel-qq-probe-") as root:
        os.environ["ASTRBOT_ROOT"] = root
        from astrbot.api.message_components import At, Plain
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.platform.platform_metadata import PlatformMetadata
        from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            PatchedGroupMessage, QQOfficialPlatformAdapter,
        )
        from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_event import QQOfficialWebhookMessageEvent
        from sentinel.qq_official_snapshot import extract

        async def run_case(author, extra, *, webhook=False):
            data = {"id": "m", "group_openid": "g", "content": "example text",
                    "author": author, "attachments": [], "mentions": [], **extra}
            raw = PatchedGroupMessage(None, "event", data)
            message = await QQOfficialPlatformAdapter._parse_from_qqofficial(raw, MessageType.GROUP_MESSAGE)
            message.group_id = raw.group_openid
            message.session_id = message.group_id
            event_class = QQOfficialWebhookMessageEvent if webhook else QQOfficialMessageEvent
            name = "qq_official_webhook" if webhook else "qq_official"
            event = event_class(message.message_str, message,
                PlatformMetadata(name=name, description="test", id="p"), message.session_id, None)
            assert event.unified_msg_origin == "p:GroupMessage:g"
            return extract(event, is_management_command=lambda text: False, text_segment_types=(Plain, At))

        async def probe():
            human = {"member_openid": "sender", "bot": False}
            accepted = await run_case(human, {"message_type": 0})
            assert accepted.status == "ok" and accepted.message.is_text is True
            assert accepted.message.sender_id == "sender" and accepted.message.session_origin == "p:GroupMessage:g"
            webhook = await run_case(human, {"message_type": 0}, webhook=True)
            assert webhook.status == "ok" and webhook.message.is_text is True
            missing_bot = await run_case({"member_openid": "sender"}, {"message_type": 0})
            assert missing_bot.status == "bot_identity_unverified" and missing_bot.message is None
            bot = await run_case({"member_openid": "sender", "bot": True}, {"message_type": 0})
            assert bot.status == "bot_message" and bot.message is None
            for extra in ({}, {"message_type": 103},
                          {"message_type": 0, "message_reference": {"message_id": "quoted"}}):
                result = await run_case(human, extra)
                assert result.status == "ok" and result.message.is_text is False

        asyncio.run(probe())
    print("Synthetic QQ adapter snapshots passed; no QQ connection or real messages used.")


if __name__ == "__main__":
    main()
