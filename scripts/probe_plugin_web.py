"""Offline plugin Web API probe using real AstrBot and Starlette request types.

Authenticated identities are simulated; no dashboard server or login is run.
The probe does not establish that the deployed dashboard authenticates safely.
"""

import asyncio
import importlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory(prefix="sentinel-web-probe-") as root:
        os.environ["ASTRBOT_ROOT"] = root
        from astrbot.api.web import PluginRequest, bind_request_context
        from starlette.requests import Request

        package = types.ModuleType("sentinel_web_probe")
        package.__path__ = [str(Path(__file__).resolve().parents[1])]
        sys.modules[package.__name__] = package
        plugin_class = importlib.import_module("sentinel_web_probe.main").RuleSentinel

        class Context:
            def __init__(self):
                self.registered_web_apis = []
            def register_web_api(self, *args):
                self.registered_web_apis.append(args)

        def make_request(username, body=b"", *, query=b"", headers=None, forbid_read=False):
            raw_headers = headers if headers is not None else [(b"content-length", str(len(body)).encode()),
                                                               (b"content-type", b"application/json")]
            scope = {"type": "http", "method": "POST", "scheme": "http", "path": "/test",
                     "query_string": query, "headers": raw_headers, "server": ("localhost", 80),
                     "client": ("localhost", 1234)}
            async def receive():
                if forbid_read:
                    raise AssertionError("request body was read")
                return {"type": "http.request", "body": body, "more_body": False}
            return PluginRequest(Request(scope, receive), username=username)

        async def call(handler, username, body=b"", **kwargs):
            with bind_request_context(make_request(username, body, **kwargs)):
                response = await handler()
            return response.status_code, json.loads(response.body)

        async def probe():
            context = Context()
            plugin = plugin_class(context, {"dashboard_admins": ["admin"]})
            await plugin.initialize()
            try:
                assert plugin.intake is None and len(context.registered_web_apis) == 26
                fields = {"platform_id": "p", "group_id": "g", "rule_id": "r", "expected_version": 0,
                          "text": "rule", "enabled": True, "allowed_actions": ["mute"],
                          "duration_mode": "strict", "severity_seconds": {"low": 30}}
                body = json.dumps(fields).encode()
                status, preview = await call(plugin.preview_rule, "admin", body)
                assert status == 200 and preview["fields"]["text"] == "rule"
                status, saved = await call(plugin.confirm_rule, "admin", json.dumps(preview).encode())
                assert status == 200 and saved["version"] == 1 and saved["modified_by"] == "admin"
                status, denied = await call(plugin.preview_rule, "api_key:synthetic", body, forbid_read=True)
                assert status == 403 and denied["status"] == "error"
                status, _ = await call(plugin.preview_rule, "admin", headers=[(b"content-type", b"application/json"),
                                       (b"content-length", b"65537")],
                                       forbid_read=True)
                assert status == 400
                status, _ = await call(plugin.preview_rule, "admin", headers=[(b"content-type", b"application/json"),
                                       (b"content-length", b"1"),
                                       (b"content-length", b"1")], forbid_read=True)
                assert status == 400
                admin, stranger = await asyncio.gather(
                    call(plugin.list_rules, "admin", query=b"platform_id=p&group_id=g"),
                    call(plugin.list_rules, "stranger", query=b"platform_id=p&group_id=g"))
                assert admin[0] == 200 and len(admin[1]) == 1 and stranger[0] == 200 and len(stranger[1]) == 1
                from astrbot.dashboard.asgi_runtime import _coerce_view_result
                with bind_request_context(make_request("admin", query=b"platform_id=p&group_id=g")):
                    response = await plugin.stream_events()
                assert await _coerce_view_result(response) is response
                chunks = asyncio.Queue()
                async def receive():
                    await asyncio.Event().wait()
                async def send(message):
                    if message["type"] == "http.response.body" and message.get("body"):
                        await chunks.put(message["body"])
                task = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send))
                try:
                    assert b"event: ping" in await asyncio.wait_for(chunks.get(), 3)
                    await asyncio.to_thread(plugin._queue.record_skip, platform_id="p", group_id="g",
                                            message_id="probe", reason="test")
                    assert b'"message_id": "probe"' in await asyncio.wait_for(chunks.get(), 3)
                finally:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                assert not plugin._event_hub.subscribers
            finally:
                await plugin.terminate()
            assert not context.registered_web_apis

        asyncio.run(probe())
    print("Synthetic plugin Web request probe passed; no dashboard server or login used.")


if __name__ == "__main__":
    main()
