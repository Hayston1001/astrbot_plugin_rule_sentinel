import json
import asyncio
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

from sentinel.jev_transport import JevRequestError, JevTransport
from sentinel.decision_errors import ContextLimitExceeded


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body
        self.content = self
        self.headers = headers or {}
        self.reads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        self.reads += 1
        yield self.body


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.request = None

    def post(self, endpoint, **kwargs):
        self.request = (endpoint, kwargs)
        return self.response


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_openrouter_uses_systemone_endpoint(self):
        session = FakeSession(FakeResponse(200, b'{"answers":{}}'))
        send = JevTransport(session, api_key="key", timeout_seconds=1, provider="openrouter")
        await send({"model": "typesafe/jev-latest"})
        self.assertEqual(session.request[0], "https://openrouter.ai/api/v1/systemone")
        self.assertNotIn("chat/completions", session.request[0])
        with self.assertRaises(ValueError):
            JevTransport(session, api_key="key", timeout_seconds=1, provider="unknown")
    async def test_request_is_https_and_does_not_follow_redirects(self):
        session = FakeSession(FakeResponse(200, json.dumps({"answers": {}}).encode()))
        send = JevTransport(session, api_key="secret", timeout_seconds=1)
        self.assertEqual(await send({"model": "m"}), {"answers": {}})
        self.assertEqual(session.request[0], "https://api.typesafe.ai/v1/systemone")
        self.assertFalse(session.request[1]["allow_redirects"])
        self.assertFalse(session.request[1]["auto_decompress"])
        self.assertEqual(session.request[1]["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(session.request[1]["headers"]["Accept-Encoding"], "identity")

    async def test_errors_do_not_include_response_contents(self):
        session = FakeSession(FakeResponse(401, b"secret message"))
        send = JevTransport(session, api_key="secret", timeout_seconds=1)
        with self.assertRaises(JevRequestError) as error:
            await send({"model": "m"})
        self.assertNotIn("secret", str(error.exception))

    async def test_invalid_json_and_oversized_response(self):
        for body in (b"bad json", b"x" * (JevTransport.MAX_RESPONSE_BYTES + 1)):
            send = JevTransport(FakeSession(FakeResponse(200, body)), api_key="key", timeout_seconds=1)
            with self.assertRaises(JevRequestError):
                await send({"model": "m"})

    async def test_explicit_context_limit_is_classified_without_details(self):
        body = json.dumps({"detail": {"code": "max_tokens_exceeded", "message": "private"}}).encode()
        send = JevTransport(FakeSession(FakeResponse(400, body)), api_key="key", timeout_seconds=1)
        with self.assertRaises(ContextLimitExceeded) as error:
            await send({"model": "m"})
        self.assertNotIn("private", str(error.exception))

    async def test_ambiguous_and_non_utf8_json_are_rejected(self):
        bodies = (b'{"answers":{},"answers":{"decision":{}}}',
                  b'{"answers":{"decision":{"choice":"review","choice":"no_action"}}}',
                  b'{"answers":{},"confidence":NaN}', '{"answers":{}}'.encode("utf-16"))
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(JevRequestError):
                await JevTransport(FakeSession(FakeResponse(200, body)), api_key="key", timeout_seconds=1)({"model": "m"})

    async def test_encoded_or_oversized_declared_response_is_not_read(self):
        for headers in ({"Content-Encoding": "gzip"}, {"Content-Length": str(JevTransport.MAX_RESPONSE_BYTES + 1)},
                        {"Content-Length": "invalid"}):
            response = FakeResponse(200, b"{}", headers)
            with self.subTest(headers=headers), self.assertRaises(JevRequestError):
                await JevTransport(FakeSession(response), api_key="key", timeout_seconds=1)({"model": "m"})
            self.assertEqual(response.reads, 0)

    @unittest.skipUnless(importlib.util.find_spec("aiohttp"), "aiohttp is not installed")
    async def test_aiohttp_loopback_dry_run(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "probe_jev_local_http.py"
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("loopback dry-run probe passed", result.stdout)

    async def test_duplicate_response_headers_are_rejected(self):
        class Headers:
            def __init__(self, duplicate):
                self.duplicate = duplicate
            def getall(self, name, default):
                return ["identity", "identity"] if name == self.duplicate else default
        for name in ("Content-Encoding", "Content-Length"):
            response = FakeResponse(200, b"{}", Headers(name))
            with self.subTest(name=name), self.assertRaises(JevRequestError):
                await JevTransport(FakeSession(response), api_key="key", timeout_seconds=1)({"model": "m"})
            self.assertEqual(response.reads, 0)
