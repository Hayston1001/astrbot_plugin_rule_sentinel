"""Exercise the experimental request guard at a mock SDK transport boundary.

No real model, network, provider configuration or credentials are used. This is
not integrated into the plugin runtime and is not a production transport.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix="sentinel-guard-probe-") as root:
        os.environ["ASTRBOT_ROOT"] = root
        import httpx
        from openai import AsyncOpenAI
        from sentinel.astrbot_llm import make_tool_set
        from sentinel.openai_request_builder import build_openai_request
        from sentinel.openai_request_guard import (MAX_RESPONSE_BYTES, SingleRequestGuard,
                                                   RequestIntegrityError, make_guarded_transport)

        async def probe():
            rules = [{"rule_id": "r", "text": "no spam", "allowed_actions": ["mute"],
                      "duration_mode": "strict", "severity_seconds": {"low": 60}}]
            for strategy in ("model", "priority"):
                request = build_openai_request(model="synthetic", message="example message", strategy=strategy, rules=rules)
                assert request["tools"] == make_tool_set(strategy).openai_schema()
            expected = build_openai_request(model="synthetic", message="example message", strategy="model", rules=rules)
            endpoint = "https://example.invalid/v1/chat/completions"
            delivered = []
            def new_http(*, redirect=False, response_factory=None):
                async def handler(request):
                    delivered.append(str(request.url))
                    assert request.headers.get("accept-encoding") == "identity"
                    if redirect:
                        return httpx.Response(307, headers={"location": "https://other.invalid/v1/chat/completions"})
                    if response_factory is not None:
                        return response_factory()
                    return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
                        "model": "synthetic", "choices": [{"index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "NO_ACTION"}}]})
                guard = SingleRequestGuard(endpoint=endpoint, expected=expected)
                transport = make_guarded_transport(delegate=httpx.MockTransport(handler), guard=guard,
                                                   api_key="synthetic")
                return httpx.AsyncClient(transport=transport, follow_redirects=True)
            http = new_http()
            client = AsyncOpenAI(api_key="synthetic", base_url="https://example.invalid/v1", http_client=http, max_retries=0)
            try:
                await client.chat.completions.create(**expected)
                try:
                    await client.chat.completions.create(**expected)
                except Exception:
                    pass
                else:
                    raise AssertionError("duplicate request was accepted")
            finally:
                await client.close()

            for kwargs in ({"extra_body": {"tools": []}}, {"extra_headers": {"Host": "other.invalid"}},
                           {"extra_headers": {"Authorization": "Bearer wrong"}},
                           {"extra_headers": {"Content-Length": "1"}},
                           {"extra_headers": {"Cookie": "session=synthetic"}},
                           {"extra_headers": {"X-Forwarded-Host": "other.invalid"}},
                           {"extra_headers": {"X-HTTP-Method-Override": "GET"}}):
                client = AsyncOpenAI(api_key="synthetic", base_url="https://example.invalid/v1",
                                     http_client=new_http(), max_retries=0)
                try:
                    try:
                        await client.chat.completions.create(**expected, **kwargs)
                    except Exception:
                        pass
                    else:
                        raise AssertionError("invalid request was accepted")
                finally:
                    await client.close()

            async with new_http() as changed_sni:
                try:
                    await changed_sni.post(endpoint, json=expected, headers={"Authorization": "Bearer synthetic"},
                                           extensions={"sni_hostname": "other.invalid"})
                except RequestIntegrityError:
                    pass
                else:
                    raise AssertionError("overridden TLS hostname was accepted")

            async with new_http() as changed:
                try:
                    await changed.post("https://other.invalid/v1/chat/completions", json=expected,
                                       headers={"Authorization": "Bearer synthetic"})
                except RequestIntegrityError:
                    pass
                else:
                    raise AssertionError("changed destination was accepted")
            async with new_http() as streaming:
                async def chunks():
                    yield b"{}"
                try:
                    await streaming.post(endpoint, content=chunks(), headers={"Authorization": "Bearer synthetic",
                                                                              "Content-Type": "application/json"})
                except RequestIntegrityError:
                    pass
                else:
                    raise AssertionError("streaming request was accepted")
                try:
                    await streaming.post(endpoint, json=expected, headers={"Authorization": "Bearer synthetic"})
                except RequestIntegrityError:
                    pass
                else:
                    raise AssertionError("streaming request did not consume attempt")
            async with new_http(redirect=True) as redirected:
                try:
                    await redirected.post(endpoint, json=expected, headers={"Authorization": "Bearer synthetic"})
                except RequestIntegrityError:
                    pass
                else:
                    raise AssertionError("redirect was accepted")
            assert delivered == [endpoint, endpoint]

            class Stream(httpx.AsyncByteStream):
                def __init__(self, chunks):
                    self.chunks = chunks
                    self.closed = False
                    self.reads = 0
                async def __aiter__(self):
                    for chunk in self.chunks:
                        self.reads += 1
                        yield chunk
                async def aclose(self):
                    self.closed = True

            async def response_case(source, headers, should_pass):
                async with new_http(response_factory=lambda: httpx.Response(200, headers=headers, stream=source)) as client:
                    try:
                        response = await client.post(endpoint, json=expected, headers={"Authorization": "Bearer synthetic"})
                    except RequestIntegrityError:
                        assert not should_pass
                    else:
                        assert should_pass and len(response.content) == MAX_RESPONSE_BYTES
                assert source.closed

            exact = Stream([b"x" * MAX_RESPONSE_BYTES])
            await response_case(exact, {}, True)
            oversized = Stream([b"x" * (MAX_RESPONSE_BYTES // 2 + 1)] * 2)
            await response_case(oversized, {}, False)
            assert oversized.reads == 2
            declared = Stream([b"unused"])
            await response_case(declared, {"content-length": str(MAX_RESPONSE_BYTES + 1)}, False)
            assert declared.reads == 0
            compressed = Stream([b"unused"])
            await response_case(compressed, {"content-encoding": "gzip"}, False)
            assert compressed.reads == 0
            invalid_length = Stream([b"unused"])
            await response_case(invalid_length, {"content-length": "invalid"}, False)
            assert invalid_length.reads == 0
            started = asyncio.Event()
            class WaitingStream(Stream):
                async def __aiter__(self):
                    started.set()
                    yield b"first"
                    await asyncio.Event().wait()
            waiting = WaitingStream([])
            async with new_http(response_factory=lambda: httpx.Response(200, stream=waiting)) as client:
                task = asyncio.create_task(client.post(endpoint, json=expected,
                                                      headers={"Authorization": "Bearer synthetic"}))
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("response read was not cancelled")
            assert waiting.closed

        asyncio.run(probe())
    print("Experimental request guard passed at mock SDK transport; no network used.")


if __name__ == "__main__":
    main()
