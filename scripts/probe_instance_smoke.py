"""Exercise an isolated AstrBot core and the installed plugin without network access."""

import asyncio
import io
import json
import os
import socket
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def run(root):
    os.environ["ASTRBOT_ROOT"] = str(root)
    sys.path.insert(0, str(root))
    from scripts.package_plugin import build_archive
    from sentinel.instance_lock import InstanceLock
    from sentinel.rule_store import RuleStore

    plugin = root / "data" / "plugins" / "astrbot_plugin_rule_sentinel"
    plugin.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(build_archive())) as archive:
        archive.extractall(plugin)
    config_dir = root / "data" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "astrbot_plugin_rule_sentinel_config.json").write_text(json.dumps({
        "monitoring_mode": "allowlist",
        "monitoring_groups": ["synthetic-platform/synthetic-group"],
        "decision_backend": "llm", "llm_provider_id": "local-stub",
    }), encoding="utf-8")
    data = root / "data" / "plugin_data" / "astrbot_plugin_rule_sentinel"
    data.mkdir(parents=True)
    rules = RuleStore(data / "sentinel.sqlite3")
    rules.initialize()
    rules.save(platform_id="synthetic-platform", group_id="synthetic-group", rule_id="r",
               expected_version=0, text="No spam", enabled=True, allowed_actions=["mute"],
               duration_mode="strict", severity_seconds={"low": 60}, modified_by="admin")

    from astrbot.core import LogBroker, db_helper
    import astrbot.core.core_lifecycle as lifecycle_module
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
    from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import PatchedGroupMessage, QQOfficialPlatformAdapter

    async def no_update():
        pass

    async def stub(**kwargs):
        assert kwargs["chat_provider_id"] == "local-stub"
        return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["propose_action"],
                               tools_call_args=[{"violated": True, "rule_id": "r", "severity": "low", "reason": "spam"}])

    def no_network(*args, **kwargs):
        raise RuntimeError("network disabled by smoke probe")

    with patch.object(socket.socket, "connect", no_network), patch.object(socket, "getaddrinfo", no_network), \
         patch.object(lifecycle_module, "update_llm_metadata", no_update):
        core = AstrBotCoreLifecycle(LogBroker(), db_helper)
        await core.initialize()
        core.star_context.llm_generate = stub
        metadata = core.star_context.get_registered_star("astrbot_plugin_rule_sentinel")
        assert metadata and metadata.star_cls and metadata.star_cls.host, "plugin host did not start"
        instance = metadata.star_cls
        core._load()
        try:
            sent = []
            completed = {}
            for scheduler in core.pipeline_scheduler_mapping.values():
                execute = scheduler.execute

                async def record_execution(event, execute=execute):
                    try:
                        await execute(event)
                    finally:
                        completed.setdefault(event.message_obj.message_id, asyncio.Event()).set()

                scheduler.execute = record_execution

            async def make_event(message_id):
                raw = PatchedGroupMessage(None, "event", {"id": message_id, "group_openid": "synthetic-group",
                    "content": "synthetic spam", "author": {"member_openid": "synthetic-sender", "bot": False},
                    "attachments": [], "mentions": [], "message_type": 0})
                message = await QQOfficialPlatformAdapter._parse_from_qqofficial(raw, MessageType.GROUP_MESSAGE)
                message.group_id = message.session_id = "synthetic-group"
                event = QQOfficialMessageEvent(message.message_str, message,
                    PlatformMetadata(name="qq_official", description="test", id="synthetic-platform"),
                    "synthetic-group", None)

                async def record_send(*args, **kwargs):
                    sent.append((args, kwargs))

                event.send = record_send
                return event

            event = await make_event("synthetic-message")
            await core.event_queue.put(event)
            await asyncio.wait_for(completed.setdefault("synthetic-message", asyncio.Event()).wait(), 20)
            for _ in range(200):
                rows = await asyncio.to_thread(instance.management.queue.list_events,
                    platform_id="synthetic-platform", group_id="synthetic-group")
                if any(row["status"] == "proposed" for row in rows):
                    break
                await asyncio.sleep(0.1)
            assert [row["status"] for row in rows] == ["started", "proposed"], rows
            assert not sent
            observed = (event.get_result(), event.is_stopped(), event.is_at_or_wake_command)
            old_host = instance.host
            ok, error = await core.plugin_manager.reload("astrbot_plugin_rule_sentinel")
            assert ok, error
            assert old_host.closed and old_host.lock._file is None
            assert old_host.intake.task.done() and not old_host.runtime._tasks and not old_host.runtime._admissions
            metadata = core.star_context.get_registered_star("astrbot_plugin_rule_sentinel")
            assert metadata and metadata.star_cls and metadata.star_cls.host
            new_host = metadata.star_cls.host
            await core.plugin_manager.uninstall_plugin("astrbot_plugin_rule_sentinel")
            assert new_host.closed and new_host.lock._file is None
            assert new_host.intake.task.done() and not new_host.runtime._tasks and not new_host.runtime._admissions
            lock = InstanceLock(data / "sentinel.lock")
            assert lock.acquire(), "lock was not released after uninstall"
            lock.release()
            baseline = await make_event("baseline-message")
            await core.event_queue.put(baseline)
            await asyncio.wait_for(completed.setdefault("baseline-message", asyncio.Event()).wait(), 20)
            assert (baseline.get_result(), baseline.is_stopped(), baseline.is_at_or_wake_command) == observed
            assert not sent
        finally:
            await core.stop()


def main():
    with tempfile.TemporaryDirectory(prefix="sentinel-instance-") as temp:
        asyncio.run(run(Path(temp)))
    print("Isolated instance smoke passed.")


if __name__ == "__main__":
    main()
