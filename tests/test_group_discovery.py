import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sentinel.group_discovery import discover_groups


def platform(identifier, kind="qq_official", scenes=None):
    return types.SimpleNamespace(meta=lambda: types.SimpleNamespace(id=identifier, name=kind),
                                 _session_scene=scenes or {})


class GroupDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_history_aliases_and_scope_validation(self):
        first, second = "a" * 32, "b" * 32
        db = types.SimpleNamespace(get_umo_aliases=AsyncMock(return_value=[
            types.SimpleNamespace(umo=f"qq1:GroupMessage:{first}", user_alias="测试群", auto_name="群名"),
            types.SimpleNamespace(umo=f"qq2:GroupMessage:{second}", user_alias="", auto_name="另一个群"),
        ]))
        context = types.SimpleNamespace(platform_manager=types.SimpleNamespace(get_insts=lambda: [
            platform("qq1", scenes={first: "group", "friend": "friend", "channel": "channel", "../bad": "group"}),
            platform("qq2", "qq_official_webhook"), platform("other", "telegram", {second: "group"}),
        ]), get_db=lambda: db)
        rows = [("qq1", f"qq1:GroupMessage:{first}"), ("qq2", f"qq2:GroupMessage:{second}"),
                ("qq1", f"qq1:FriendMessage:{second}"), ("qq1", "qq1:GroupMessage:123456"),
                ("qq1", f"qq2:GroupMessage:{first}"), ("other", f"other:GroupMessage:{second}")]
        with patch("sentinel.group_discovery._history_umos", new=AsyncMock(return_value=rows)):
            result = await discover_groups(context)
        self.assertTrue(result["history_available"])
        self.assertTrue(result["platforms_available"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["groups"], [
            {"platform_id": "qq1", "group_id": first, "name": "测试群"},
            {"platform_id": "qq2", "group_id": second, "name": "另一个群"},
        ])
        self.assertEqual(len(db.get_umo_aliases.await_args.args[0]), 2)

    async def test_no_official_platform_reports_unavailable(self):
        context = types.SimpleNamespace(platform_manager=types.SimpleNamespace(get_insts=lambda: [platform("tg", "telegram")]))
        result = await discover_groups(context)
        self.assertFalse(result["platforms_available"])
        self.assertEqual(result["groups"], [])

    async def test_history_failure_keeps_live_groups_and_reports_incomplete(self):
        context = types.SimpleNamespace(platform_manager=types.SimpleNamespace(get_insts=lambda: [
            platform("qq", scenes={"live": "group"})]), get_db=lambda: object())
        with patch("sentinel.group_discovery._history_umos", new=AsyncMock(side_effect=RuntimeError("private"))):
            result = await discover_groups(context)
        self.assertFalse(result["history_available"])
        self.assertEqual(result["groups"], [{"platform_id": "qq", "group_id": "live", "name": ""}])
        self.assertNotIn("private", repr(result))

    async def test_history_limit_is_reported(self):
        db = types.SimpleNamespace(get_umo_aliases=AsyncMock(return_value=[]))
        context = types.SimpleNamespace(platform_manager=types.SimpleNamespace(get_insts=lambda: [platform("qq")]),
                                        get_db=lambda: db)
        rows = [("qq", f"qq:GroupMessage:{'a' * 32}"), ("qq", f"qq:GroupMessage:{'b' * 32}")]
        with patch("sentinel.group_discovery.MAX_HISTORY_GROUPS", 1), \
             patch("sentinel.group_discovery._history_umos", new=AsyncMock(return_value=rows)):
            result = await discover_groups(context)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["groups"]), 1)

    @unittest.skipUnless(importlib.util.find_spec("astrbot") and importlib.util.find_spec("sqlalchemy"),
                         "AstrBot database dependencies are unavailable")
    async def test_history_query_reads_distinct_group_sessions_for_selected_platforms(self):
        # AstrBot imports can initialize data under its root. Keep them in a
        # separate process and temporary root instead of touching this checkout.
        code = '''
import asyncio, sys
from types import SimpleNamespace
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
sys.path.insert(0, sys.argv[1])
from sentinel.group_discovery import _history_umos
async def main():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE platform_message_history (platform_id TEXT, user_id TEXT, content TEXT)"))
            await connection.execute(text("INSERT INTO platform_message_history VALUES (:p, :u, :content)"), [
                {"p": "qq", "u": "qq:GroupMessage:" + "a" * 32, "content": "secret"},
                {"p": "qq", "u": "qq:GroupMessage:" + "a" * 32, "content": "another secret"},
                {"p": "qq", "u": "qq:FriendMessage:x", "content": "private"},
                {"p": "other", "u": "other:GroupMessage:" + "b" * 32, "content": "other"},
            ])
        rows = await _history_umos(SimpleNamespace(get_db=async_sessionmaker(engine)), ["qq"])
        assert rows == [("qq", "qq:GroupMessage:" + "a" * 32)], rows
    finally:
        await engine.dispose()
asyncio.run(main())
'''
        with tempfile.TemporaryDirectory() as temp:
            env = {**os.environ, "ASTRBOT_ROOT": temp, "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable, "-B", "-c", code, str(Path(__file__).resolve().parent.parent)],
                                    cwd=temp, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
