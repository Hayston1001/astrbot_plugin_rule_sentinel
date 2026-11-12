import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sentinel.qq_official_snapshot import extract


class Plain:
    pass


class At:
    pass


class Image:
    pass


class SnapshotTests(unittest.TestCase):
    def event(self, **changes):
        obj = SimpleNamespace(raw_message=SimpleNamespace(group_openid="g", raw_data={"author": {"member_openid": "sender", "bot": False}, "message_type": 0}), group_id="g",
                              message_id="m", message=[Plain(), At()], message_str="text", self_id="qq_official")
        for key, value in changes.items():
            setattr(obj, key, value)
        return SimpleNamespace(message_obj=obj, get_platform_name=lambda: "qq_official",
                               get_platform_id=lambda: "p", get_sender_id=lambda: "sender",
                               unified_msg_origin="p:GroupMessage:g")

    def test_placeholder_self_id_is_not_used_as_proof(self):
        result = extract(self.event(raw_message=SimpleNamespace(group_openid="g", raw_data={"author": {"member_openid": "sender"}})), is_management_command=lambda _: False, text_segment_types=(Plain, At))
        self.assertEqual(result.status, "bot_identity_unverified")
        self.assertIsNone(result.message)

    def test_text_and_at_are_accepted_without_event_reference(self):
        result = extract(self.event(), is_management_command=lambda _: False, text_segment_types=(Plain, At))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.message.sender_id, "sender")
        self.assertTrue(result.message.is_text)

    def test_message_time_uses_platform_timestamp_without_fabrication(self):
        now = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
        event = self.event()
        event.message_obj.raw_message.raw_data["timestamp"] = now
        result = extract(event, is_management_command=lambda _: False, text_segment_types=(Plain, At))
        self.assertTrue(result.message.message_time)
        event.message_obj.raw_message.raw_data["timestamp"] = "invalid"
        result = extract(event, is_management_command=lambda _: False, text_segment_types=(Plain, At))
        self.assertIsNone(result.message.message_time)

    def test_media_and_unverified_group(self):
        result = extract(self.event(message=[Plain(), Image()]), is_management_command=lambda _: False, text_segment_types=(Plain, At))
        self.assertFalse(result.message.is_text)
        result = extract(self.event(group_id="different"), is_management_command=lambda _: False, text_segment_types=(Plain, At))
        self.assertEqual(result.status, "not_verified_qq_group")

    def test_bot_and_sender_mismatch_are_skipped(self):
        for author, status in (({"member_openid": "sender", "bot": True}, "bot_message"),
                               ({"member_openid": "other", "bot": False}, "sender_identity_unverified")):
            raw = SimpleNamespace(group_openid="g", raw_data={"author": author})
            result = extract(self.event(raw_message=raw), is_management_command=lambda _: False, text_segment_types=(Plain, At))
            self.assertEqual(result.status, status)

    def test_raw_rich_payload_and_unknown_type_are_not_treated_as_text(self):
        author = {"member_openid": "sender", "bot": False}
        for fields in ({}, {"message_type": 103}, {"message_type": 0, "attachments": [{"url": "image"}]},
                       {"message_type": 0, "ark_data": {"title": "card"}}):
            with self.subTest(fields=fields):
                raw = SimpleNamespace(group_openid="g", raw_data={"author": author, **fields})
                result = extract(self.event(raw_message=raw), is_management_command=lambda _: False, text_segment_types=(Plain, At))
                self.assertFalse(result.message.is_text)
