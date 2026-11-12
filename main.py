"""AstrBot entry point: dashboard management and group message monitoring."""

import asyncio
import json
import re
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .sentinel.active_group_index import ActiveGroupIndex
from .sentinel.async_io import run_sync_safely
from .sentinel.dry_run_host import DryRunHost, LockInUseError
from .sentinel.dry_run_ingest import MAX_MESSAGE_CHARS
from .sentinel.dry_run_queue import DryRunQueue
from .sentinel.event_intake import Skip
from .sentinel.event_stream import EventHub
from .sentinel.management_service import ManagementService
from .sentinel.plugin_settings import decision_model_unconfigured, read_settings
from .sentinel.qq_official_snapshot import extract
from .sentinel.message_entry import is_activated_command, strip_wake_prefix
from .sentinel.jev_models import list_jev_models
from .sentinel.rule_store import RuleStore, VersionConflict
from .sentinel.settings_store import DEFAULTS, SettingsStore, capture_legacy_config, validate_settings


PLUGIN_NAME = "astrbot_plugin_rule_sentinel"
MAX_BODY_BYTES = 65536
MAX_JSON_DEPTH = 64

# AstrBot normalizes the legacy configuration after importing this module but
# before constructing the plugin. Preserve it before the empty schema is applied.
_plugin_data_root = Path(get_astrbot_plugin_data_path())
capture_legacy_config(_plugin_data_root / PLUGIN_NAME,
                      _plugin_data_root.parent / "config" / f"{PLUGIN_NAME}_config.json")


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("non-finite JSON value")


def _bounded_json_depth(raw):
    depth = 0
    quoted = False
    escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return False
        elif char in (93, 125):
            depth -= 1
            if depth < 0:
                return False
    return not quoted and depth == 0


def _all_groups_query(query):
    if "all_groups" not in query:
        return False
    if query.get("all_groups") != "true" or "platform_id" in query or "group_id" in query:
        raise ValueError("invalid group scope")
    return True


class RuleSentinel(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.management = None
        self.group_index = None
        self.host = None
        self.intake = None
        self.start_error = None
        self.settings_store = None
        self._settings_lock = asyncio.Lock()
        self._closing = False
        self._rules = None
        self._queue = None
        self._event_hub = None
        self._retention_task = None
        self._lock_path = None
        for route, handler, methods in (
            ("rules", self.list_rules, ["GET"]),
            ("rules/history", self.rule_history, ["GET"]),
            ("rules/preview", self.preview_rule, ["POST"]),
            ("rules/confirm", self.confirm_rule, ["POST"]),
            ("audit", self.list_audit, ["GET"]),
            ("messages", self.list_messages, ["GET"]),
            ("message-content", self.message_content, ["GET"]),
            ("execute-mute", self.execute_mute, ["POST"]),
            ("execute-action", self.execute_mute, ["POST"]),
            ("events", self.stream_events, ["GET"]),
            ("notifications", self.list_notifications, ["GET"]),
            ("status", self.status, ["GET"]),
            ("platforms", self.list_platforms, ["GET"]),
            ("groups", self.list_groups, ["GET"]),
            ("group-register", self.register_group, ["POST"]),
            ("group-alias", self.save_group_alias, ["POST"]),
            ("group-name", self.get_group_name, ["POST"]),
            ("summary", self.summary, ["GET"]),
            ("ui-state", self.get_ui_state, ["GET"]),
            ("ui-state", self.save_ui_state, ["POST"]),
            ("settings", self.get_settings, ["GET"]),
            ("settings", self.save_settings, ["POST"]),
            ("group-monitoring", self.set_group_monitoring, ["POST"]),
            ("group-settings", self.save_group_settings, ["POST"]),
            ("providers", self.list_providers, ["GET"]),
            ("jev/models", self.list_jev_models, ["POST"]),
        ):
            context.register_web_api(f"/{PLUGIN_NAME}/{route}", handler, methods, route)

    async def initialize(self):
        self.start_error = None
        data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        await asyncio.to_thread(data_dir.mkdir, parents=True, exist_ok=True)
        database = data_dir / "sentinel.sqlite3"
        rules, queue = RuleStore(database), DryRunQueue(database)
        await asyncio.to_thread(rules.initialize)
        await asyncio.to_thread(queue.initialize)
        self.settings_store = SettingsStore(database, data_dir / "legacy-settings.json")
        self.config = await asyncio.to_thread(self.settings_store.load_or_migrate, dict(self.config))
        self._rules, self._queue, self._lock_path = rules, queue, data_dir / "sentinel.lock"
        self._event_hub = EventHub(asyncio.get_running_loop())
        queue.event_sink = self._event_hub.publish
        self.group_index = ActiveGroupIndex(rules)
        await asyncio.to_thread(self.group_index.reload)
        self.management = ManagementService(rules, queue,
            duration_limit=lambda: self.config.get("max_duration_seconds", 2592000), group_index=self.group_index)
        try:
            await run_sync_safely(queue.purge_message_contents)
        except Exception:
            logger.error("Rule Sentinel could not clean expired message contents.")
        self._retention_task = asyncio.create_task(self._clean_message_contents())
        await self._start_monitoring()

    async def _clean_message_contents(self):
        while True:
            await asyncio.sleep(3600)
            try:
                await run_sync_safely(self._queue.purge_message_contents)
            except Exception:
                logger.error("Rule Sentinel could not clean expired message contents.")

    async def _start_monitoring(self):
        self.start_error = None
        try:
            validate_settings(self.config)
            if decision_model_unconfigured(self.config):
                self.start_error = "model_not_configured"
                logger.info("Rule Sentinel monitoring is waiting for a decision model.")
                return
            read_settings(self.config)
        except (ValueError, TypeError):
            self.start_error = "invalid_configuration"
            logger.error("Rule Sentinel monitoring disabled: invalid configuration.")
            return
        try:
            host = DryRunHost(context=self.context, config=self.config, rules=self._rules,
                              queue=self._queue, lock_path=self._lock_path)
            await host.start()
        except LockInUseError:
            self.start_error = "lock_in_use"
            logger.error("Rule Sentinel could not start dry-run monitoring: lock in use.")
            return
        except Exception as error:
            self.start_error = "start_failed"
            logger.error(f"Rule Sentinel could not start dry-run monitoring: {type(error).__name__}")
            return
        self.host = host
        self.intake = host.intake
        logger.info("Rule Sentinel monitoring is active.")

    async def _stop_monitoring(self):
        host, self.intake = self.host, None
        if host is not None:
            await host.close()
            self.host = None

    async def terminate(self):
        self._closing = True
        if self._retention_task is not None:
            self._retention_task.cancel()
            try:
                await self._retention_task
            except asyncio.CancelledError:
                pass
            self._retention_task = None
        if self._event_hub is not None:
            self._event_hub.close()
        async with self._settings_lock:
            await self._stop_monitoring()
        self.management = None
        self.group_index = None
        routes = getattr(self.context, "registered_web_apis", None)
        if isinstance(routes, list):
            # Remove only this instance's handlers. A newer plugin instance or
            # another plugin may already have replaced a route on reload.
            routes[:] = [entry for entry in routes
                         if not (len(entry) >= 2 and entry[0].startswith(f"/{PLUGIN_NAME}/")
                                 and getattr(entry[1], "__self__", None) is self)]

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def capture_group_message(self, event: AstrMessageEvent):
        """Copy an admitted group message into the dry-run inbox.

        AstrBot activates this handler for ordinary group messages, but the
        pipeline only calls handlers after the whitelist, session, rate-limit
        and content-safety stages, so a message the framework rejected is never
        seen here. The handler only copies; it never sends a reply, sets a
        result or stops the event, so the rest of the pipeline is unaffected.
        """
        intake = self.intake
        if intake is None:
            return
        if is_activated_command(event, self.capture_group_message):
            return
        try:
            admission = intake.runtime.admission
            if admission.settings_match is not None and not admission.settings_match():
                return
            obj = event.message_obj
            key = (event.get_platform_id(), getattr(obj, "group_id", None))
            policy = admission.policy
            if (policy.mode == "allowlist" and key not in policy.groups) or (policy.mode == "blocklist" and key in policy.groups):
                return
            if self.group_index is None or not self.group_index.is_enabled(*key):
                return
            # This version exposes management only through the dashboard;
            # no plugin chat-management commands are registered.
            result = extract(event, is_management_command=lambda text: False,
                             text_segment_types=(Plain, At))
            if result.message is not None:
                from dataclasses import replace
                result_message = replace(result.message, text=strip_wake_prefix(event, self.context, result.message.text, At))
                if len(result_message.text) > MAX_MESSAGE_CHARS or len(result.message.text) > MAX_MESSAGE_CHARS:
                    intake.offer(Skip(result_message.platform_id, result_message.group_id,
                                      result_message.message_id, "message_too_long", result_message.session_origin))
                else:
                    intake.offer(result_message)
            elif result.status != "unsupported_platform":
                platform_id, group_id = event.get_platform_id(), getattr(obj, "group_id", None)
                message_id = getattr(obj, "message_id", None)
                if all(isinstance(value, str) and value.strip() for value in (platform_id, group_id, message_id)):
                    intake.offer(Skip(platform_id, group_id, message_id, result.status,
                                      getattr(event, "unified_msg_origin", None)))
        except Exception:
            logger.error("Rule Sentinel could not capture a group message.")

    async def _run(self, operation, *args):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        try:
            return json_response(await asyncio.to_thread(operation, *args))
        except PermissionError:
            return error_response("Not authorized", status_code=403)
        except VersionConflict:
            return error_response("Rule changed; preview again", status_code=409)
        except (ValueError, TypeError, KeyError):
            return error_response("Invalid request", status_code=400)
        except Exception:
            logger.error("Rule Sentinel management operation failed")
            return error_response("Operation failed", status_code=500)

    async def _payload(self):
        # The public AstrBot 4.29.3 API exposes body() but not a bounded stream.
        # Reject an oversized declared length before reading where possible.
        headers = getattr(request, "headers", {})
        getlist = getattr(headers, "getlist", None)
        if callable(getlist):
            content_types = getlist("content-type")
            if len(content_types) != 1:
                return None
            content_type = content_types[0]
            values = getlist("content-length")
            if len(values) > 1:
                return None
            length = values[0] if values else None
        else:
            content_type = headers.get("content-type")
            length = headers.get("content-length")
        # Browsers can submit text/plain forms without a CORS preflight. Only
        # accept the JSON media type used by the dashboard bridge; this reduces
        # form-based CSRF exposure but does not replace deployment CORS policy.
        if (not isinstance(content_type, str) or len(content_type) > 256
                or content_type.split(";", 1)[0].strip().lower() != "application/json"):
            return None
        if length is not None:
            if (not isinstance(length, str) or len(length) > 10 or not length.isascii()
                    or not length.isdigit() or int(length) > MAX_BODY_BYTES):
                return None
        raw = await request.body()
        if (len(raw) > MAX_BODY_BYTES or (length is not None and len(raw) != int(length))
                or not _bounded_json_depth(raw)):
            return None
        try:
            result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object,
                                parse_constant=_reject_json_constant)
            return result if isinstance(result, dict) else None
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None

    async def list_rules(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        return await self._run(self.management.list_rules, request.username,
                               request.query.get("platform_id"), request.query.get("group_id"))

    async def rule_history(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        return await self._run(self.management.rule_history, request.username,
            request.query.get("platform_id"), request.query.get("group_id"), request.query.get("rule_id"))

    async def preview_rule(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        if not self.management.actor_allowed(request.username):
            return error_response("Not authorized", status_code=403)
        data = await self._payload()
        if data is None:
            return error_response("Invalid request", status_code=400)
        return await self._run(self.management.preview, request.username, data)

    async def confirm_rule(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        if not self.management.actor_allowed(request.username):
            return error_response("Not authorized", status_code=403)
        data = await self._payload()
        if data is None:
            return error_response("Invalid request", status_code=400)
        return await self._run(self.management.confirm, request.username, data)

    async def list_audit(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        try:
            after_id = int(request.query.get("after_id", "0"))
            limit = int(request.query.get("limit", "50"))
        except (TypeError, ValueError):
            return error_response("Invalid pagination", status_code=400)
        return await self._run(self.management.list_audit, request.username,
            request.query.get("platform_id"), request.query.get("group_id"), after_id, limit)

    async def list_messages(self):
        """Return newest messages first; before_id is an exclusive latest-event upper bound."""
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        try:
            all_groups = _all_groups_query(request.query)
            if "after_id" in request.query:
                return error_response("Invalid pagination", status_code=400)
            raw_before = request.query.get("before_id")
            before_id = None if raw_before is None else int(raw_before)
            limit = int(request.query.get("limit", "50"))
        except (TypeError, ValueError):
            return error_response("Invalid pagination", status_code=400)
        return await self._run(self.management.list_messages, request.username,
            request.query.get("platform_id"), request.query.get("group_id"), before_id, limit, all_groups)

    async def message_content(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        return await self._run(self.management.message_content, request.username,
                               request.query.get("platform_id"), request.query.get("group_id"), request.query.get("message_id"))

    async def stream_events(self):
        if self.management is None or self._event_hub is None or self._closing:
            return error_response("Service not initialized", status_code=503)
        if not self.management.actor_allowed(request.username):
            return error_response("Not authorized", status_code=403)
        platform = request.query.get("platform_id")
        group = request.query.get("group_id")
        try:
            all_groups = _all_groups_query(request.query)
        except ValueError:
            return error_response("Invalid group identity", status_code=400)
        if not all_groups and any(not isinstance(value, str) or not value.strip() or len(value) > 128 for value in (platform, group)):
            return error_response("Invalid group identity", status_code=400)
        try:
            subscription = self._event_hub.subscribe(platform, group)
        except RuntimeError:
            return error_response("Subscription limit reached", status_code=429)
        from starlette.responses import StreamingResponse
        class EventResponse(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    subscription.close()
        return EventResponse(subscription.stream(self._queue), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def status(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        if not self.management.actor_allowed(request.username):
            return error_response("Not authorized", status_code=403)
        try:
            validate_settings(self.config)
            if decision_model_unconfigured(self.config):
                return json_response({"monitoring": "disabled", "dry_run_only": True,
                                      "action_execution_enabled": self.config["action_execution_enabled"],
                                      "action_execution_available": False,
                                      "backend": self.config["decision_backend"] or "none", "start_error": "model_not_configured"})
            settings = read_settings(self.config)
        except (ValueError, TypeError):
            return json_response({"monitoring": "invalid_configuration", "dry_run_only": True,
                                  "action_execution_enabled": self.config.get("action_execution_enabled") is True,
                                  "action_execution_available": False,
                                  "backend": "none",
                                  "start_error": "invalid_configuration"})
        execution_available = (self.config.get("action_execution_enabled") is True and self.host is not None
                               and getattr(self.host, "mute_executor", None) is not None)
        return json_response({"monitoring": "dry_run" if self.intake is not None else "disabled",
                              "dry_run_only": not execution_available,
                              "action_execution_enabled": self.config.get("action_execution_enabled") is True,
                              "action_execution_available": execution_available,
                              "backend": settings.backend or "none", "start_error": self.start_error})

    async def execute_mute(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        if not self.management.actor_allowed(request.username):
            return error_response("Not authorized", status_code=403)
        data = await self._payload()
        if not isinstance(data, dict) or set(data) != {"intent_id"} or type(data["intent_id"]) is not int or data["intent_id"] <= 0:
            return error_response("Invalid execution request", status_code=400)
        host = self.host
        if self.config.get("action_execution_enabled") is not True or host is None or host.mute_executor is None:
            return error_response("Execution disabled or unavailable", status_code=503)
        try:
            result = await host.confirm_mute(data["intent_id"])
            return json_response({"status": result})
        except (ValueError, TypeError):
            return error_response("Invalid execution request", status_code=400)
        except Exception:
            logger.error("Rule Sentinel action confirmation failed")
            return error_response("Execution unavailable", status_code=500)

    async def list_notifications(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        try:
            after_id = int(request.query.get("after_id", "0"))
            limit = int(request.query.get("limit", "50"))
        except (TypeError, ValueError):
            return error_response("Invalid pagination", status_code=400)
        return await self._run(self.management.list_notifications, request.username, after_id, limit)

    async def list_platforms(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        if not self.management.actor_allowed(request.username):
            return error_response("Not authorized", status_code=403)
        try:
            result = []
            for platform in list(self.context.platform_manager.get_insts()):
                metadata = platform.meta()
                if (metadata.name in ("qq_official", "qq_official_webhook")
                        and isinstance(metadata.id, str) and metadata.id.strip()):
                    result.append({"id": metadata.id, "type": metadata.name})
            return json_response(result)
        except Exception:
            logger.error("Rule Sentinel could not list platform instances")
            return error_response("Operation failed", status_code=500)

    async def list_groups(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        configured = set(self.config.get("monitoring_groups", [])) | set(self.config.get("group_overrides", {}))
        return await self._run(self.management.list_groups, request.username, configured)

    async def save_group_alias(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        if not self.management.actor_allowed(request.username):
            return error_response("Not authorized", status_code=403)
        data = await self._payload()
        return await self._run(self.management.save_group_name, request.username, data)

    async def register_group(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        if not self.management.actor_allowed(request.username):
            return error_response("Not authorized", status_code=403)
        data = await self._payload()
        return await self._run(self.management.ensure_group, request.username, data)

    async def get_group_name(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        if not self.management.actor_allowed(request.username):
            return error_response("Not authorized", status_code=403)
        data = await self._payload()
        if (not isinstance(data, dict) or set(data) != {"platform_id", "group_id"}
                or any(not isinstance(value, str) or not value.strip() or len(value) > 128 for value in data.values())
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", data["group_id"]) is None):
            return error_response("Invalid request", status_code=400)
        try:
            platform = next((p for p in self.context.platform_manager.get_insts()
                             if p.meta().id == data["platform_id"] and
                             p.meta().name in ("qq_official", "qq_official_webhook")), None)
            if platform is None:
                return error_response("Platform unavailable", status_code=404)
            from botpy.http import Route
            route = Route("GET", "/v2/groups/{group_openid}/info", group_openid=data["group_id"])
            result = await asyncio.wait_for(platform.get_client().api._http.request(route), timeout=8)
            name = result.get("group_name") if isinstance(result, dict) else None
            if not isinstance(name, str) or not name.strip():
                return error_response("Group name unavailable", status_code=502)
            saved = await asyncio.to_thread(self.management.save_group_name, request.username,
                                            {**data, "name": name}, platform_name=True)
            return json_response(saved)
        except Exception:
            return error_response("Group name unavailable", status_code=502)

    async def summary(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        try:
            all_groups = _all_groups_query(request.query)
        except ValueError:
            return error_response("Invalid group identity", status_code=400)
        return await self._run(self.management.summarize_group, request.username,
                               request.query.get("platform_id"), request.query.get("group_id"), all_groups)

    async def get_ui_state(self):
        return await self._run(self.management.get_ui_state, request.username) if self.management else error_response(
            "Service not initialized", status_code=503)

    async def save_ui_state(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        if not self.management.actor_allowed(request.username):
            return error_response("Not authorized", status_code=403)
        data = await self._payload()
        if data is None:
            return error_response("Invalid request", status_code=400)
        return await self._run(self.management.save_ui_state, request.username, data)

    def _public_settings(self):
        result = {key: value for key, value in self.config.items() if key != "jev_api_key"}
        result["jev_api_key_set"] = bool(self.config.get("jev_api_key", ""))
        return result

    async def get_settings(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        if not self.management.actor_allowed(request.username):
            return error_response("Not authorized", status_code=403)
        return json_response(self._public_settings())

    async def list_jev_models(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        if not self.management.actor_allowed(request.username):
            return error_response("Not authorized", status_code=403)
        data = await self._payload()
        if not isinstance(data, dict) or set(data) - {"provider", "api_key"} or "provider" not in data:
            return error_response("Invalid request", status_code=400)
        provider, key = data["provider"], data.get("api_key", "")
        if not isinstance(key, str):
            return error_response("Invalid request", status_code=400)
        key = key or self.config.get("jev_api_key", "")
        try:
            if provider == "openrouter":
                models = await list_jev_models(provider, key)
            else:
                import aiohttp
                async with aiohttp.ClientSession(trust_env=False) as session:
                    models = await list_jev_models(provider, key, session=session)
            return json_response({"models": models})
        except (ValueError, TypeError):
            return error_response("Invalid request", status_code=400)
        except Exception:
            return error_response("Model request failed", status_code=502)

    async def _change_settings(self, data, group_change=False, group_settings=False):
        async with self._settings_lock:
            if self._closing:
                raise RuntimeError("plugin is closing")
            candidate = dict(self.config)
            if group_settings:
                from .sentinel.group_settings import GROUP_KEYS, validate_group_key
                if (set(data) != {"platform_id", "group_id", "overrides"}
                        or not isinstance(data["overrides"], dict) or set(data["overrides"]) - GROUP_KEYS):
                    raise ValueError("invalid group settings")
                platform, group = data["platform_id"], data["group_id"]
                if not isinstance(platform, str) or not isinstance(group, str):
                    raise ValueError("invalid group identity")
                key = f"{platform}/{group}"
                validate_group_key(key)
                overrides = dict(candidate["group_overrides"])
                if data["overrides"]:
                    overrides[key] = dict(data["overrides"])
                else:
                    overrides.pop(key, None)
                candidate["group_overrides"] = overrides
            elif group_change:
                if set(data) != {"platform_id", "group_id", "enabled"} or type(data["enabled"]) is not bool:
                    raise ValueError("invalid group setting")
                platform, group = data["platform_id"], data["group_id"]
                if any(not isinstance(value, str) or not value.strip() or "/" in value or len(value) > 128
                       or value != value.strip() for value in (platform, group)):
                    raise ValueError("invalid group identity")
                entry = f"{platform}/{group}"
                groups = list(candidate["monitoring_groups"])
                should_list = data["enabled"] == (candidate["monitoring_mode"] == "allowlist")
                groups = [item for item in groups if item != entry]
                if should_list:
                    groups.append(entry)
                candidate["monitoring_groups"] = groups
            else:
                if not data or set(data) - (set(DEFAULTS) - {"group_overrides"}):
                    raise ValueError("invalid settings")
                updates = dict(data)
                if updates.get("jev_api_key") == "":
                    updates.pop("jev_api_key")
                candidate.update(updates)
            validate_settings(candidate)
            remember_group = f"{data['platform_id']}/{data['group_id']}" if group_change or group_settings else None
            changed = {key for key in candidate if candidate[key] != self.config.get(key)}
            if candidate != self.config or remember_group is not None:
                await asyncio.to_thread(self.settings_store.save, candidate, remember_group=remember_group)
            if changed == {"llm_no_action_reason_enabled"}:
                self.config["llm_no_action_reason_enabled"] = candidate["llm_no_action_reason_enabled"]
            elif candidate != self.config:
                self.config.clear()
                self.config.update(candidate)
                try:
                    await self._stop_monitoring()
                except Exception:
                    self.start_error = "stop_failed"
                    raise
                await self._start_monitoring()
            elif self.start_error and self.host is None:
                await self._start_monitoring()
            return {"settings": self._public_settings(), "monitoring": "dry_run" if self.intake else "disabled",
                    "start_error": self.start_error}

    async def _settings_request(self, group_change=False, group_settings=False):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        if not self.management.actor_allowed(request.username):
            return error_response("Not authorized", status_code=403)
        data = await self._payload()
        if data is None:
            return error_response("Invalid request", status_code=400)
        task = asyncio.create_task(self._change_settings(data, group_change, group_settings))
        # A disconnected dashboard must not cancel an in-progress host transition.
        task.add_done_callback(lambda finished: None if finished.cancelled() else finished.exception())
        try:
            return json_response(await asyncio.shield(task))
        except (ValueError, TypeError, KeyError):
            return error_response("Invalid settings", status_code=400)
        except Exception:
            logger.error("Rule Sentinel settings update failed")
            return error_response("Settings update failed", status_code=500)

    async def save_settings(self):
        return await self._settings_request()

    async def set_group_monitoring(self):
        return await self._settings_request(group_change=True)

    async def save_group_settings(self):
        return await self._settings_request(group_settings=True)

    async def list_providers(self):
        if self.management is None:
            return error_response("Service not initialized", status_code=503)
        if not self.management.actor_allowed(request.username):
            return error_response("Not authorized", status_code=403)
        try:
            providers = self.context.get_all_providers()
            return json_response([{"id": item.provider_config["id"]} for item in providers
                                  if isinstance(item.provider_config.get("id"), str)])
        except Exception:
            return error_response("Provider list unavailable", status_code=500)
