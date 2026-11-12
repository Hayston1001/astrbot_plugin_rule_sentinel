"""Offline synthetic QQ callbacks through the actual adapter and commit queue.

This neither connects QQ nor exercises the full AstrBot message pipeline.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix="sentinel-qq-tap-probe-") as root:
        os.environ["ASTRBOT_ROOT"] = root
        from astrbot.api.message_components import At, Plain
        from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
            PatchedGroupMessage, QQOfficialPlatformAdapter,
        )
        from sentinel.platform_tap import PlatformTap
        from sentinel.qq_official_snapshot import extract

        async def run_case(*, with_tap, mention=False, bot=False, observer_fails=False):
            queue = asyncio.Queue()
            adapter = QQOfficialPlatformAdapter({"id": "p", "appid": "synthetic", "secret": "synthetic",
                "enable_group_c2c": True, "enable_guild_direct_message": False}, {}, queue)
            observed, errors = [], []
            def observe(event):
                snapshot = extract(event, is_management_command=lambda text: False, text_segment_types=(Plain, At))
                observed.append(snapshot)
                if observer_fails:
                    raise ValueError("synthetic observer failure")
            tap = PlatformTap(adapter, observe, lambda: errors.append(True)) if with_tap else None
            if tap:
                tap.install()
            raw = PatchedGroupMessage(None, "event", {"id": "m", "group_openid": "g", "content": "hello",
                "author": {"member_openid": "sender", "bot": bot}, "attachments": [], "mentions": [],
                "message_type": 0})
            callback = adapter.client.on_group_at_message_create if mention else adapter.client.on_group_message_create
            await callback(raw)
            event = queue.get_nowait()
            state = (event.get_platform_id(), event.get_sender_id(), event.unified_msg_origin,
                     event.message_obj.message_str, tuple(type(part).__name__ for part in event.message_obj.message),
                     event.is_wake, event.is_stopped(), adapter._session_scene.copy(),
                     adapter._session_last_message_id.copy())
            if tap:
                tap.close()
                assert "commit_event" not in vars(adapter)
            return state, observed, errors, event

        async def probe():
            for mention in (False, True):
                for bot in (False, True):
                    baseline, _, _, _ = await run_case(with_tap=False, mention=mention, bot=bot)
                    state, snapshots, errors, event = await run_case(with_tap=True, mention=mention, bot=bot)
                    assert state == baseline and errors == [] and len(snapshots) == 1
                    assert snapshots[0].status == ("bot_message" if bot else "ok")
                    if not bot:
                        snapshot = snapshots[0].message
                        assert snapshot.text == "hello" and snapshot.session_origin == "p:GroupMessage:g"
                        event.message_obj.message_str = "changed later"
                        assert snapshot.text == "hello"
                    failed, _, errors, _ = await run_case(with_tap=True, mention=mention, bot=bot, observer_fails=True)
                    assert failed == baseline and errors == [True]

            queue = asyncio.Queue(maxsize=1)
            config = {"id": "p", "appid": "synthetic", "secret": "synthetic",
                      "enable_group_c2c": True, "enable_guild_direct_message": False}
            adapter = QQOfficialPlatformAdapter(config, {}, queue)
            seen = []
            tap = PlatformTap(adapter, seen.append, lambda: None)
            tap.install()
            raw = PatchedGroupMessage(None, "event", {"id": "m", "group_openid": "g", "content": "hello",
                "author": {"member_openid": "sender", "bot": False}, "attachments": [], "mentions": [],
                "message_type": 0})
            await adapter.client.on_group_message_create(raw)
            try:
                await adapter.client.on_group_message_create(raw)
            except asyncio.QueueFull:
                pass
            else:
                raise AssertionError("queue failure was suppressed")
            assert len(seen) == 1 and queue.get_nowait() is seen[0]
            tap.close()

            # A platform replacement is a distinct object; the old wrapper
            # cannot silently observe messages on the replacement instance.
            replacement_queue = asyncio.Queue()
            replacement = QQOfficialPlatformAdapter(config, {}, replacement_queue)
            await replacement.client.on_group_message_create(raw)
            assert replacement_queue.qsize() == 1 and len(seen) == 1

        asyncio.run(probe())
    print("Synthetic QQ callback and commit tap probe passed; no QQ connection or full pipeline used.")


if __name__ == "__main__":
    main()
