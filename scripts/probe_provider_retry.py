"""Reproduce AstrBot OpenAI-compatible provider fallbacks without network.

The real provider method runs with a fake query implementation and synthetic
input. This probe documents unsafe fallback behavior; it does not enable the
provider or establish behavior for other versions and provider types.
"""

import asyncio
import copy
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix="sentinel-provider-probe-") as root:
        os.environ["ASTRBOT_ROOT"] = root
        from astrbot.core.provider.entities import LLMResponse
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        import httpx
        from openai import AsyncOpenAI
        from sentinel.llm_protocol import normalize_response

        class FakeTools:
            def empty(self):
                return False

            def get_func_desc_openai_style(self, **kwargs):
                return [{"type": "function", "function": {"name": "propose_mute", "parameters": {"type": "object"}}}]

        class FakeClient:
            api_key = None

        async def run_case(error):
            provider = object.__new__(ProviderOpenAIOfficial)
            provider.api_keys = ["synthetic"]
            provider.provider_config = {}
            provider.client = FakeClient()
            provider.get_model = lambda: "synthetic-model"
            attempts = []
            async def prepare(*args, **kwargs):
                messages = [{"role": "system", "content": "system"},
                            {"role": "user", "content": "rules and message"}]
                return {"messages": messages, "tools": [{"type": "function"}]}, messages
            async def query(payloads, tools, **kwargs):
                attempts.append((copy.deepcopy(payloads), tools, kwargs))
                if len(attempts) == 1:
                    raise ValueError(error)
                return LLMResponse(role="assistant", completion_text="NO_ACTION")
            provider._prepare_chat_payload = prepare
            provider._query = query
            result = await provider.text_chat(prompt="synthetic", func_tool=FakeTools(),
                                              contexts=[], request_max_retries=0)
            assert normalize_response(result).status == "no_action"
            assert len(attempts) == 2 and all(item[2]["request_max_retries"] == 0 for item in attempts)
            return attempts

        async def probe():
            context_attempts = await run_case("maximum context length exceeded")
            assert any(message["role"] == "user" for message in context_attempts[0][0]["messages"])
            assert not any(message["role"] == "user" for message in context_attempts[1][0]["messages"])
            tool_attempts = await run_case("Function calling is not enabled")
            assert "tools" in tool_attempts[0][0] and tool_attempts[0][1] is not None
            assert "tools" not in tool_attempts[1][0] and tool_attempts[1][1] is None

            requests = []
            async def handler(request):
                requests.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
                    "model": "synthetic", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "NO_ACTION"}}]})
            http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            client = AsyncOpenAI(api_key="synthetic", base_url="https://example.invalid/v1", http_client=http,
                                 max_retries=0)
            provider = object.__new__(ProviderOpenAIOfficial)
            provider.client = client
            provider.reasoning_key = "reasoning_content"
            provider.default_params = inspect.signature(client.chat.completions.create).parameters.keys()
            provider.provider_config = {"custom_extra_body": {
                "messages": [{"role": "system", "content": "replacement"}], "tools": []}}
            try:
                result = await provider._query({"model": "synthetic", "messages": [
                    {"role": "system", "content": "system"}, {"role": "user", "content": "rules and message"}]},
                    FakeTools(), request_max_retries=1)
            finally:
                await client.close()
            assert normalize_response(result).status == "no_action"
            assert len(requests) == 1
            assert requests[0]["messages"] == [{"role": "system", "content": "replacement"}]
            assert requests[0]["tools"] == []

        asyncio.run(probe())
    print("Synthetic provider fallbacks reproduced; payload override reproduced with mock transport; no network used.")


if __name__ == "__main__":
    main()
