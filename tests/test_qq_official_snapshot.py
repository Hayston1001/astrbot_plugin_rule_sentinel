import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sentinel.qq_official_snapshot import extract


class Plain:
    pass


class At:
    pass


class Image:
    def __init__(self, file=None, url=None):
        self.file = file
        self.url = url


class Record:
    pass


class Reply:
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
                       {"message_type": 0, "ark_data": {"title": "card"}},
                       {"message_type": 0, "msg_elements": [{"content": "nested"}]}):
            with self.subTest(fields=fields):
                raw = SimpleNamespace(group_openid="g", raw_data={"author": author, **fields})
                result = extract(self.event(raw_message=raw), is_management_command=lambda _: False, text_segment_types=(Plain, At))
                self.assertFalse(result.message.is_text)

    def test_quoted_text_is_accepted_without_the_quoted_content(self):
        raw = SimpleNamespace(group_openid="g", raw_data={
            "author": {"member_openid": "sender", "bot": False},
            "message_type": 103,
            "msg_elements": [{"id": "quoted", "content": "quoted words"}],
        })
        event = self.event(raw_message=raw, message=[Reply(), Plain()], message_str="reply text")
        result = extract(event, is_management_command=lambda _: False,
                         text_segment_types=(Plain, At), quote_segment_types=(Reply,))
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.message.is_text)
        self.assertEqual(result.message.text, "reply text")

    def test_mentions_are_rendered_with_display_names(self):
        raw = SimpleNamespace(group_openid="g", raw_data={
            "author": {"member_openid": "sender", "bot": False},
            "message_type": 0,
            "mentions": [
                {"id": "id-xiaoming", "member_openid": "member-xiaoming", "username": " 小明 "},
                {"id": "id-xiaohong", "username": "   "},
            ],
        })
        text = '你好 <@member-xiaoming>, <@id-xiaohong>, <@all> 和 <qqbot-at-user id="id-xiaoming" />'
        event = self.event(raw_message=raw, message_str=text)
        result = extract(event, is_management_command=lambda _: False, text_segment_types=(Plain, At))
        self.assertEqual(result.message.text, "你好 @小明, <@id-xiaohong>, <@all> 和 @小明")
        self.assertEqual(result.message.original_text, result.message.text)
        self.assertEqual(event.message_obj.message_str, text)

    def test_quote_segment_stays_non_text_until_the_caller_identifies_it(self):
        raw = SimpleNamespace(group_openid="g", raw_data={
            "author": {"member_openid": "sender", "bot": False},
            "message_type": 103,
            "msg_elements": [{"id": "quoted", "content": "quoted words"}],
        })
        event = self.event(raw_message=raw, message=[Reply(), Plain()], message_str="reply text")
        result = extract(event, is_management_command=lambda _: False, text_segment_types=(Plain, At))
        self.assertFalse(result.message.is_text)

    def test_quote_with_its_own_media_is_still_not_text(self):
        raw = SimpleNamespace(group_openid="g", raw_data={
            "author": {"member_openid": "sender", "bot": False},
            "message_type": 103,
            "msg_elements": [{"id": "quoted", "content": "quoted words"}],
            "attachments": [{"url": "image"}],
        })
        event = self.event(raw_message=raw, message=[Reply(), Plain(), Image()], message_str="reply text")
        result = extract(event, is_management_command=lambda _: False,
                         text_segment_types=(Plain, At), quote_segment_types=(Reply,))
        self.assertFalse(result.message.is_text)

    def image_extract(self, event):
        return extract(event, is_management_command=lambda _: False,
                       text_segment_types=(Plain, At), quote_segment_types=(Reply,),
                       image_segment_types=(Image,))

    def test_image_only_message_is_kept_for_later_judging(self):
        url = "https://cdn.example.com/a.png"
        raw = SimpleNamespace(group_openid="g", raw_data={
            "author": {"member_openid": "sender", "bot": False},
            "message_type": 0, "attachments": [{"url": url}]})
        event = self.event(raw_message=raw, message=[Image(file=url)], message_str="")
        result = self.image_extract(event)
        self.assertEqual(result.status, "ok")
        self.assertFalse(result.message.is_text)
        self.assertEqual(result.message.image_urls, (url,))
        self.assertEqual(result.message.text, "")

    def test_text_and_image_keep_both_parts(self):
        url = "https://cdn.example.com/a.png"
        raw = SimpleNamespace(group_openid="g", raw_data={
            "author": {"member_openid": "sender", "bot": False},
            "message_type": 0, "attachments": [{"url": url}]})
        event = self.event(raw_message=raw, message=[Plain(), Image(file=url)], message_str="look at this")
        result = self.image_extract(event)
        self.assertFalse(result.message.is_text)
        self.assertEqual(result.message.text, "look at this")
        self.assertEqual(result.message.image_urls, (url,))

    def test_local_temp_image_file_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\npayload")
            raw = SimpleNamespace(group_openid="g", raw_data={
                "author": {"member_openid": "sender", "bot": False},
                "message_type": 0, "attachments": [{"url": str(path)}]})
            event = self.event(raw_message=raw, message=[Image(file=str(path))], message_str="")
            result = self.image_extract(event)
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.message.image_urls, (str(path),))
            self.assertFalse(result.message.is_text)

    def test_quote_image_is_never_attributed_to_the_sender(self):
        raw = SimpleNamespace(group_openid="g", raw_data={
            "author": {"member_openid": "sender", "bot": False},
            "message_type": 103, "msg_elements": [{"id": "quoted", "content": "quoted words"}]})
        event = self.event(raw_message=raw, message=[Reply(), Plain()], message_str="reply text")
        result = self.image_extract(event)
        self.assertTrue(result.message.is_text)
        self.assertEqual(result.message.image_urls, ())

    def test_mixed_media_and_unparsed_attachments_stay_unsupported(self):
        url = "https://cdn.example.com/a.png"
        cases = (
            ([Image(file=url), Record()], {"message_type": 0}),
            ([Image(file=url)], {"message_type": 0, "attachments": [{"url": url}, {"url": url}]}),
        )
        for chain, fields in cases:
            with self.subTest(chain=chain):
                raw = SimpleNamespace(group_openid="g", raw_data={
                    "author": {"member_openid": "sender", "bot": False}, **fields})
                event = self.event(raw_message=raw, message=chain, message_str="")
                result = self.image_extract(event)
                self.assertFalse(result.message.is_text)
                self.assertEqual(result.message.image_urls, ())

    def test_invalid_image_reference_is_rejected(self):
        raw = SimpleNamespace(group_openid="g", raw_data={
            "author": {"member_openid": "sender", "bot": False}, "message_type": 0})
        for value in ("ftp://cdn.example.com/a.png", "javascript:alert(1)", "https://cdn.example.com/a b.png"):
            with self.subTest(value=value):
                event = self.event(raw_message=raw, message=[Image(file=value)], message_str="")
                self.assertEqual(self.image_extract(event).status, "invalid_image_reference")
