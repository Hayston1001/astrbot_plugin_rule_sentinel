import json
import unittest

from sentinel.jev_models import list_jev_models
from sentinel.jev_transport import JevRequestError
from test_jev_transport import FakeResponse


class Session:
    def __init__(self, response):
        self.response, self.request = response, None

    def get(self, url, **kwargs):
        self.request = (url, kwargs)
        return self.response


class JevModelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_list_uses_documented_shape_and_returns_only_latest(self):
        body = {"models": [{"name": "jev-1.13", "description": "version", "release_date": "2026-01-01"},
                           {"name": "jev-latest", "description": "alias", "release_date": "2026-01-01"}]}
        session = Session(FakeResponse(200, json.dumps(body).encode()))
        result = await list_jev_models("typesafe", "secret-key", session=session)
        self.assertEqual(result, ["jev-latest"])
        self.assertEqual(session.request[0], "https://api.typesafe.ai/v1/models")
        self.assertEqual(session.request[1]["headers"]["Authorization"], "Bearer secret-key")
        self.assertFalse(session.request[1]["allow_redirects"])
        self.assertNotIn("secret-key", repr(result))

    async def test_invalid_or_unknown_models_response_is_rejected(self):
        bodies = [b'{}', b'{"data":[{"id":"jev-latest"}]}', b'{"models":[{"alias":"jev-latest"}]}',
                  b'{"models":[{"name":"jev-preview"}]}', b'{"models":[{"name":"jev-latest"},{}]}',
                  b'{"models":[],"models":[{"name":"jev-latest"}]}', b'not json']
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(JevRequestError) as error:
                await list_jev_models("typesafe", "secret-key", session=Session(FakeResponse(200, body)))
            self.assertNotIn("secret-key", str(error.exception))

    async def test_openrouter_is_fixed_and_never_uses_http(self):
        session = Session(None)
        self.assertEqual(await list_jev_models("openrouter", "secret-key", session=session), ["typesafe/jev-latest"])
        self.assertIsNone(session.request)

    async def test_http_error_and_malicious_response_do_not_disclose_key(self):
        session = Session(FakeResponse(401, b"secret-key"))
        with self.assertRaises(JevRequestError) as error:
            await list_jev_models("typesafe", "secret-key", session=session)
        self.assertNotIn("secret-key", str(error.exception))
