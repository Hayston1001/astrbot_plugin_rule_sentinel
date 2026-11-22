import importlib.util
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sentinel.notification_recipients import PAGE_SIZE, find_notification_recipients


@unittest.skipUnless(importlib.util.find_spec("astrbot"), "AstrBot is unavailable")
class NotificationRecipientTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_sessions_use_astrbot_metadata_for_search_and_paging(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"ASTRBOT_ROOT": root}))
        from astrbot.core.db import po
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.addAsyncCleanup(engine.dispose)
        tables = [po.ConversationV2.__table__, po.UmoAlias.__table__]
        sender = getattr(po, "UmoSender", None)
        if sender is not None:
            tables.append(sender.__table__)
        async with engine.begin() as connection:
            await connection.run_sync(lambda db: po.UmoAlias.metadata.create_all(db, tables=tables))

        db = SimpleNamespace(get_db=async_sessionmaker(engine))
        private = "test%_bot:FriendMessage:u"
        group = "test%_bot:GroupMessage:u"
        saved = [
            po.UmoAlias(umo=private, creator_sender_id="u", auto_name="小林%_", user_alias="值班"),
            po.UmoAlias(umo="other:FriendMessage:u", creator_sender_id="u", auto_name="小林"),
            po.UmoAlias(umo=group, creator_sender_id="u", auto_name="不应显示"),
            po.UmoAlias(umo="testXYbot:FriendMessage:u", creator_sender_id="u", auto_name="不应显示"),
            po.UmoAlias(umo="test%_bot:FriendMessage: ", creator_sender_id="u", auto_name="不应显示"),
            po.ConversationV2(platform_id="test%_bot", user_id=private, content=["private message"]),
            po.ConversationV2(platform_id="test%_bot", user_id="test%_bot:FriendMessage:no-name"),
        ]
        if sender is not None:
            saved.append(sender(umo=private, sender_id="u", sender_name="重复"))
            saved.append(sender(umo="test%_bot:FriendMessage:v", sender_id="v", sender_name="小张"))
            saved.append(sender(umo=group, sender_id="u", sender_name="不应显示"))
        saved += [po.UmoAlias(umo=f"test%_bot:FriendMessage:u{i}", creator_sender_id=f"u{i}", auto_name=f"用户{i}")
                  for i in range(PAGE_SIZE + 1)]
        async with db.get_db() as session:
            session.add_all(saved)
            await session.commit()

        async def find(search="", page=1, selected=""):
            return await find_notification_recipients(db, {"test%_bot": "telegram"}, search=search,
                                                      page=page, selected=selected)

        result = await find(search="小林", selected=private)
        self.assertEqual(result["recipients"], [result["selected"]])
        self.assertEqual(result["selected"], {"target": private, "nickname": "小林%_", "alias": "值班",
                                             "platform": "test%_bot", "platform_type": "telegram", "session_id": "u"})
        self.assertEqual((await find(search="值班"))["recipients"], result["recipients"])
        self.assertEqual((await find(search="不应显示", selected=group))["selected"], None)
        self.assertEqual((await find(search="不应显示"))["recipients"], [])
        self.assertEqual((await find(search="no-name"))["recipients"][0]["nickname"], "")
        # SQL wildcards entered by the user remain literal.
        self.assertEqual((await find(search="小林%_"))["recipients"], result["recipients"])
        self.assertEqual((await find(search="小林_"))["recipients"], [])
        if sender is not None:
            self.assertEqual((await find(search="小张"))["recipients"][0]["session_id"], "v")
        labels = await find_notification_recipients(db, {"test%_bot": "telegram"}, page=None,
                                                   targets=(private, "other:FriendMessage:u", group))
        self.assertEqual(labels, result["recipients"])
        first, second = await find(selected=private), await find(page=2, selected=private)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(len(first["recipients"]), PAGE_SIZE)
        self.assertEqual(len(second["recipients"]), 4 if sender else 3)
        self.assertFalse({row["target"] for row in first["recipients"]} &
                         {row["target"] for row in second["recipients"]})

    async def test_recorded_sender_name_fills_a_missing_private_nickname(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"ASTRBOT_ROOT": root}))
        from astrbot.core.db import po
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.addAsyncCleanup(engine.dispose)
        tables = [po.ConversationV2.__table__, po.UmoAlias.__table__]
        if getattr(po, "UmoSender", None) is not None:
            tables.append(po.UmoSender.__table__)
        if getattr(po, "PlatformMessageHistory", None) is not None:
            tables.append(po.PlatformMessageHistory.__table__)
        async with engine.begin() as connection:
            await connection.run_sync(lambda db: po.UmoAlias.metadata.create_all(db, tables=tables))

        db = SimpleNamespace(get_db=async_sessionmaker(engine))
        private = "bot:FriendMessage:u1"
        unknown = "bot:FriendMessage:u2"
        saved = [po.UmoAlias(umo=private, creator_sender_id="u1", auto_name=None, user_alias=None),
                 po.UmoAlias(umo=unknown, creator_sender_id="u2", auto_name=None, user_alias=None),
                 po.UmoAlias(umo="other:FriendMessage:u1", creator_sender_id="u1", auto_name=None, user_alias=None)]
        sender = getattr(po, "UmoSender", None)
        if sender is not None:
            saved.append(sender(umo="bot:GroupMessage:g1", sender_id="u1", sender_name="群内昵称"))
            # The same identifier on another platform instance must not leak in.
            saved.append(sender(umo="other:GroupMessage:g1", sender_id="u1", sender_name="其他实例"))
        history = getattr(po, "PlatformMessageHistory", None)
        if history is not None:
            saved.append(history(platform_id="bot", user_id="bot:GroupMessage:g1", sender_id="u2",
                                 sender_name="历史昵称", content="ignored"))
        async with db.get_db() as session:
            session.add_all(saved)
            await session.commit()

        rows = await find_notification_recipients(db, {"bot": "qq_official"}, page=1)
        names = {row["target"]: row["nickname"] for row in rows["recipients"]}
        self.assertEqual(names[private], "群内昵称" if sender is not None else "")
        self.assertEqual(names[unknown], "历史昵称" if history is not None else "")
        expected_other = "其他实例" if sender is not None else ""
        self.assertEqual((await find_notification_recipients(db, {"other": "qq_official"}, page=1))
                         ["recipients"][0]["nickname"], expected_other)
