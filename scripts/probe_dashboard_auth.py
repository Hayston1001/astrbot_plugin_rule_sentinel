"""Isolated real AstrBot routing and auth probe; not a deployed login test."""

import asyncio
import importlib
import os
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace


def main():
    with tempfile.TemporaryDirectory(prefix="sentinel-auth-probe-") as root:
        os.environ["ASTRBOT_ROOT"] = root
        import httpx
        import jwt
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from astrbot.dashboard.api.plugins import legacy_router, router
        from astrbot.dashboard.responses import ApiError
        from astrbot.dashboard.services.api_key_service import ApiKeyService
        from astrbot.dashboard.services.auth_service import DASHBOARD_JWT_COOKIE_NAME

        package = types.ModuleType("sentinel_auth_probe")
        package.__path__ = [str(Path(__file__).resolve().parents[1])]
        sys.modules[package.__name__] = package
        plugin_class = importlib.import_module("sentinel_auth_probe.main").RuleSentinel

        class Context:
            def __init__(self):
                self.registered_web_apis = []

            def register_web_api(self, *args):
                self.registered_web_apis.append(args)

        class Database:
            async def get_active_api_key_by_hash(self, key_hash):
                if key_hash == ApiKeyService.hash_key("synthetic-key"):
                    return SimpleNamespace(key_id="synthetic", scopes=["plugin"])
                if key_hash == ApiKeyService.hash_key("limited-key"):
                    return SimpleNamespace(key_id="limited", scopes=[])
                return None

            async def touch_api_key(self, key_id):
                assert key_id == "synthetic"

        async def probe():
            context = Context()
            plugin = plugin_class(context, {"dashboard_admins": ["admin", "api_key:synthetic"]})
            await plugin.initialize()
            try:
                app = FastAPI()
                app.state.jwt_secret = "synthetic-secret-not-used-by-a-deployment"
                app.state.db = Database()
                app.state.core_lifecycle = SimpleNamespace(star_context=context)
                app.include_router(legacy_router)
                app.include_router(router, prefix="/api/v1")

                @app.exception_handler(ApiError)
                async def api_error(request, exc):
                    return JSONResponse({"message": exc.message}, status_code=exc.status_code)

                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    token = lambda name: jwt.encode({"username": name}, app.state.jwt_secret, algorithm="HS256")
                    legacy = "/api/plug/astrbot_plugin_rule_sentinel/rules?platform_id=p&group_id=g"
                    modern = "/api/v1/plugins/extensions/astrbot_plugin_rule_sentinel/rules?platform_id=p&group_id=g"
                    for path in (legacy, modern):
                        assert (await client.get(path)).status_code == 401
                        assert (await client.get(path, headers={"Authorization": "Bearer invalid"})).status_code == 401
                        expired = jwt.encode({"username": "admin", "exp": int(time.time()) - 60},
                                             app.state.jwt_secret, algorithm="HS256")
                        forged = jwt.encode({"username": "admin"}, "wrong-secret-not-used-by-any-real-deployment", algorithm="HS256")
                        for invalid in (expired, forged):
                            assert (await client.get(path, headers={"Authorization": "Bearer " + invalid})).status_code == 401
                        assert (await client.get(path, headers={"Authorization": "Bearer " + token("stranger")})).status_code == 200
                        assert (await client.get(path, headers={"Authorization": "Bearer " + token("admin")})).status_code == 200
                        assert (await client.get(path, cookies={DASHBOARD_JWT_COOKIE_NAME: token("admin")})).status_code == 200
                    assert (await client.get(modern, headers={"X-API-Key": "synthetic-key"})).status_code == 403
                    assert (await client.get(modern, headers={"X-API-Key": "limited-key"})).status_code == 403
                    assert (await client.get(legacy, headers={"X-API-Key": "synthetic-key"})).status_code == 401
                    groups = "/api/v1/plugins/extensions/astrbot_plugin_rule_sentinel/groups"
                    assert (await client.get(groups)).status_code == 401
                    assert (await client.get(groups, headers={"X-API-Key": "synthetic-key"})).status_code == 403
                    assert (await client.get(groups, headers={"Authorization": "Bearer " + token("stranger")})).status_code == 200
                    summary = "/api/v1/plugins/extensions/astrbot_plugin_rule_sentinel/summary?platform_id=p&group_id=g"
                    assert (await client.get(summary)).status_code == 401
                    assert (await client.get(summary, headers={"X-API-Key": "synthetic-key"})).status_code == 403
                    assert (await client.get(summary, headers={"Authorization": "Bearer " + token("admin")})).status_code == 200
                    messages = "/api/v1/plugins/extensions/astrbot_plugin_rule_sentinel/messages?platform_id=p&group_id=g"
                    assert (await client.get(messages)).status_code == 401
                    assert (await client.get(messages, headers={"X-API-Key": "synthetic-key"})).status_code == 403
                    assert (await client.get(messages, headers={"Authorization": "Bearer " + token("admin")})).status_code == 200
                    for prefix in ("/api/plug/", "/api/v1/plugins/extensions/"):
                        ui_state = prefix + "astrbot_plugin_rule_sentinel/ui-state"
                        headers = {"Authorization": "Bearer " + token("admin")}
                        assert (await client.get(ui_state, headers=headers)).status_code == 200
                        assert (await client.post(ui_state, headers=headers, json={"platform_id": "p", "group_id": "g"})).status_code == 200
                        assert (await client.post(ui_state, headers=headers, json={"platform_id": "p"})).status_code == 400
                        assert (await client.post(ui_state, headers={"X-API-Key": "synthetic-key"}, json={"platform_id": "p", "group_id": "g"})).status_code in (401, 403)
                    for prefix in ("/api/plug/", "/api/v1/plugins/extensions/"):
                        path = prefix + "astrbot_plugin_rule_sentinel/rules/preview"
                        fields = {"platform_id": "p", "group_id": "g", "rule_id": "r", "expected_version": 0,
                                  "text": "rule", "enabled": True, "allowed_actions": ["mute"],
                                  "duration_mode": "strict", "severity_seconds": {"low": 30}}
                        assert (await client.post(path, json=fields, headers={"Authorization": "Bearer " + token("stranger")})).status_code == 200
                        assert (await client.post(path, json=fields, headers={"Authorization": "Bearer " + token("admin")})).status_code == 200
                        for action in ("preview", "confirm"):
                            form_path = prefix + "astrbot_plugin_rule_sentinel/rules/" + action
                            form = await client.post(form_path, content=b'{}', headers={"Origin": "https://sibling.example",
                                "Content-Type": "text/plain"}, cookies={DASHBOARD_JWT_COOKIE_NAME: token("admin")})
                            assert form.status_code == 400
            finally:
                await plugin.terminate()
            assert not context.registered_web_apis

        asyncio.run(probe())
    print("Isolated dashboard auth probe passed; deployed middleware and login not tested.")


if __name__ == "__main__":
    main()
