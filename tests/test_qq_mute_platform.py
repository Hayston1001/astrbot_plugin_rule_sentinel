import json
import unittest

from sentinel.qq_mute_platform import QQMutePlatform


class FakeResponse:
    def __init__(self, status, data):
        self.status, self.data = status, data
        self.headers = {"X-Tps-Trace-Id": "trace-1"}
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self, limit):
        return json.dumps(self.data).encode()


class FakeSession:
    def __init__(self):
        self.calls = []
        self.permission = "admin"
        self.mute_status = 200
        self.mute_data = None
        self.permission_status = 200

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("getAppAccessToken"):
            return FakeResponse(200, {"access_token": "token", "expires_in": 7200})
        if url.endswith("bot_state"):
            return FakeResponse(self.permission_status, {"member_role": self.permission})
        return FakeResponse(self.mute_status, self.mute_data or {"code": self.mute_status})


class QQMutePlatformTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_cache_and_fixed_expiry_body(self):
        session = FakeSession()
        platform = QQMutePlatform(session, lambda _: ("app", "secret"), clock=lambda: 1)
        self.assertTrue(await platform.verify_permission("p", "g"))
        result = await platform.mute("p", "g", "sender", "2026-11-02T00:01:00+00:00")
        self.assertEqual(result, {"outcome": "succeeded", "platform_code": "200", "trace_id": "trace-1"})
        self.assertEqual(len([call for call in session.calls if call[1].endswith("getAppAccessToken")]), 1)
        self.assertEqual(session.calls[-1][2]["json"], {"members": [{"op": "add", "member_openid": "sender",
            "mute_expire_at": "2026-11-02T00:01:00+00:00"}]})
        self.assertNotIn("secret", repr(result))

    async def test_permission_and_ambiguous_server_error(self):
        session = FakeSession()
        session.permission = "member"
        session.mute_status = 500
        platform = QQMutePlatform(session, lambda _: ("app", "secret"))
        self.assertFalse(await platform.verify_permission("p", "g"))
        self.assertEqual((await platform.mute("p", "g", "u", "expiry"))["outcome"], "unknown")

    async def test_error_classification(self):
        session = FakeSession()
        platform = QQMutePlatform(session, lambda _: ("app", "secret"))
        for status, data, expected, reason in (
            (400, {"code": 40103004}, "blocked", "protected_target"),
            (400, {"code": "40103004"}, "blocked", "protected_target"),
            (400, {"code": 11703, "err_code": 40011030}, "permission_denied", "permission_denied"),
            (401, {}, "permission_denied", "permission_denied"),
            (403, {}, "permission_denied", "permission_denied"),
            (400, {"code": 123}, "failed", None),
            (500, {"code": 40103004}, "unknown", None),
        ):
            with self.subTest(status=status, data=data):
                session.mute_status, session.mute_data = status, data
                result = await platform.mute("p", "g", "u", "expiry")
                self.assertEqual(result["outcome"], expected)
                self.assertEqual(result.get("reason_code"), reason)
        for status in (401, 403):
            session.permission_status = status
            self.assertIs(await platform.verify_permission("p", "g"), False)
        session.permission_status = 500
        self.assertIsNone(await platform.verify_permission("p", "g"))

    async def test_group_message_body_with_and_without_quote(self):
        session = FakeSession()
        platform = QQMutePlatform(session, lambda _: ("app", "secret"))
        await platform.send_group_message("p", "g", "u text", message_id="m", msg_seq=123, reference_id="m")
        self.assertTrue(session.calls[-1][1].endswith("/v2/groups/g/messages"))
        self.assertEqual(session.calls[-1][2]["json"], {"markdown": {"content": "u text"}, "msg_type": 2, "msg_id": "m", "msg_seq": 123, "message_reference": {"message_id": "m"}})
        await platform.send_group_message("p", "g", "text", markdown=False)
        self.assertEqual(session.calls[-1][2]["json"], {"content": "text", "msg_type": 0})
        for status, expected in ((403, "failed"), (408, "unknown"), (500, "unknown"), (302, "unknown")):
            session.mute_status = status
            self.assertEqual((await platform.send_group_message("p", "g", "text"))["outcome"], expected)
        session.mute_status, session.mute_data = 400, {"code": 40034005}
        self.assertEqual((await platform.send_group_message("p", "g", "text", message_id="m", msg_seq=1))["reason_code"], "quote_expired")
