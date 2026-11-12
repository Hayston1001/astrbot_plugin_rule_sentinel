"""Run one synthetic dry-run against the explicitly configured real provider.

Manual only: this makes a billable network request. Never add it to test discovery.
"""

import asyncio
import io
import json
import logging
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_ID = "deepseek/deepseek-flash"
SOURCE_ID = "deepseek"
sys.path.insert(0, str(ROOT))


def load_provider():
    path = Path(os.environ.get("SENTINEL_PROVIDER_CONFIG", str(Path.home() / ".astrbot" / "data" / "cmd_config.json")))
    if not path.is_file():
        raise FileNotFoundError("provider configuration file not found")
    with path.open(encoding="utf-8-sig") as source:
        config = json.load(source)
    providers = [item for item in config.get("provider", []) if item.get("id") == PROVIDER_ID]
    sources = [item for item in config.get("provider_sources", []) if item.get("id") == SOURCE_ID]
    if (len(providers) != 1 or len(sources) != 1 or providers[0].get("provider_source_id") != SOURCE_ID
            or sources[0].get("type") != "openai_chat_completion"):
        raise ValueError("configured provider or source is missing, duplicated, or incompatible")
    return {"provider": providers, "provider_sources": sources}


async def run(root, provider_config):
    os.environ["ASTRBOT_ROOT"] = str(root)
    sys.path.insert(0, str(root))
    from scripts.package_plugin import build_archive
    from sentinel.rule_store import RuleStore

    data = root / "data"
    plugin = data / "plugins" / "astrbot_plugin_rule_sentinel"
    plugin.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(build_archive())) as archive:
        archive.extractall(plugin)
    (data / "cmd_config.json").write_text(json.dumps(provider_config), encoding="utf-8")
    config_dir = data / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "astrbot_plugin_rule_sentinel_config.json").write_text(json.dumps({
        "monitoring_mode": "allowlist",
        "monitoring_groups": ["synthetic-platform/synthetic-group"],
        "decision_backend": "llm", "llm_provider_id": PROVIDER_ID,
        "model_timeout_seconds": 90,
    }), encoding="utf-8")
    state = data / "plugin_data" / "astrbot_plugin_rule_sentinel"
    state.mkdir(parents=True)
    rules = RuleStore(state / "sentinel.sqlite3")
    rules.initialize()
    rules.save(platform_id="synthetic-platform", group_id="synthetic-group", rule_id="r",
               expected_version=0, text="Do not post unsolicited advertisements.", enabled=True,
               allowed_actions=["mute"], duration_mode="strict", severity_seconds={"low": 60},
               modified_by="synthetic-admin")

    # Suppress framework and HTTP logs, which may contain provider errors or credentials.
    logging.disable(logging.CRITICAL)
    from astrbot.core import LogBroker, db_helper, html_renderer
    from loguru import logger as loguru_logger
    loguru_logger.remove()
    import astrbot.core.core_lifecycle as lifecycle_module
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
    from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import PatchedGroupMessage, QQOfficialPlatformAdapter

    async def no_update():
        pass

    result = {"provider_id": PROVIDER_ID, "tool_call": None, "error_category": None, "http_status": None}
    with patch.object(lifecycle_module, "update_llm_metadata", no_update), patch.object(html_renderer, "initialize", no_update):
        core = AstrBotCoreLifecycle(LogBroker(), db_helper)
        await core.initialize()
        try:
            metadata = core.star_context.get_registered_star("astrbot_plugin_rule_sentinel")
            if not metadata or not metadata.star_cls or not metadata.star_cls.host:
                raise RuntimeError("dry-run host did not start")
            instance = metadata.star_cls
            provider = core.provider_manager.inst_map.get(PROVIDER_ID)
            if provider is None:
                raise RuntimeError("provider was not loaded")

            original_query = provider._query
            original_generate = core.star_context.llm_generate
            queries = 0

            async def one_query(*args, **kwargs):
                nonlocal queries
                queries += 1
                if queries != 1:
                    raise RuntimeError("additional model request blocked")
                kwargs["request_max_retries"] = 1
                return await original_query(*args, **kwargs)

            async def no_fallback(error, *args, **kwargs):
                raise error

            async def observe_generate(*args, **kwargs):
                try:
                    response = await original_generate(*args, **kwargs)
                    result["tool_call"] = bool(getattr(response, "tools_call_name", []))
                    return response
                except Exception as error:
                    result["error_category"] = type(error).__name__
                    status = getattr(error, "status_code", None)
                    if type(status) is int:
                        result["http_status"] = status
                    raise

            provider._query = one_query
            provider._handle_api_error = no_fallback
            core.star_context.llm_generate = observe_generate
            core._load()
            raw = PatchedGroupMessage(None, "event", {"id": "synthetic-message", "group_openid": "synthetic-group",
                "content": "Hello everyone, have a nice day.",
                "author": {"member_openid": "synthetic-sender", "bot": False},
                "attachments": [], "mentions": [], "message_type": 0})
            message = await QQOfficialPlatformAdapter._parse_from_qqofficial(raw, MessageType.GROUP_MESSAGE)
            message.group_id = message.session_id = "synthetic-group"
            event = QQOfficialMessageEvent(message.message_str, message,
                PlatformMetadata(name="qq_official", description="test", id="synthetic-platform"),
                "synthetic-group", None)
            await core.event_queue.put(event)
            rows = []
            for _ in range(1200):
                rows = await asyncio.to_thread(instance.management.queue.list_events,
                    platform_id="synthetic-platform", group_id="synthetic-group")
                if rows and rows[-1]["status"] != "started":
                    break
                await asyncio.sleep(0.1)
            result["audit_statuses"] = [row["status"] for row in rows]
            result["decision_status"] = rows[-1]["status"] if rows else "missing"
            try:
                detail = json.loads(rows[-1]["detail"]) if rows else {}
            except (ValueError, TypeError):
                detail = {}
            for key in ("provider_id", "resolved_model", "message_chars", "model_latency_ms"):
                if key in detail:
                    result[key] = detail[key]
            if result["decision_status"] not in ("proposed", "no_action", "review"):
                reason = rows[-1]["detail"] if rows else "audit_timeout"
                result["reason"] = reason if re.fullmatch(r"[a-z_]{1,80}", reason) else "unavailable"
            result["request_count"] = queries
            return result
        finally:
            if hasattr(core, "curr_tasks"):
                await core.stop()


def main():
    try:
        provider_config = load_provider()
        with tempfile.TemporaryDirectory(prefix="sentinel-real-model-") as temp:
            result = asyncio.run(run(Path(temp), provider_config))
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0 if result["decision_status"] in ("proposed", "no_action", "review") and result["request_count"] == 1 else 1
    except Exception as error:
        reason = ("provider_config_not_found" if isinstance(error, FileNotFoundError)
                  else "invalid_provider_configuration" if isinstance(error, (ValueError, json.JSONDecodeError))
                  else "probe_failed")
        print(json.dumps({"error_category": type(error).__name__, "reason": reason}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
