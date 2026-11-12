"""Experimental isolated OpenAI-compatible caller, not connected to runtime.

Transport and response parsing are injected so offline tests can use a mock.
Provider identity, endpoint ownership, TLS, DNS and proxy trust must be
established separately before any real use. No chat tool or action is exposed.
"""

import asyncio
import math

from .openai_request_builder import build_openai_request
from .openai_request_guard import SingleRequestGuard, _endpoint, make_guarded_transport
from .decision_errors import ContextLimitExceeded


class GuardedModelCallError(Exception):
    """Safe-to-log failure without request contents or provider credentials."""


def _is_context_limit_error(error) -> bool:
    if getattr(error, "status_code", None) != 400:
        return False
    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        return False
    inner = body.get("error")
    if isinstance(inner, dict):
        body = inner
    code = body.get("code")
    return isinstance(code, str) and code in {"context_length_exceeded", "model_context_window_exceeded",
                                                "max_tokens_exceeded"}


class GuardedOpenAIInvoker:
    def __init__(self, *, provider_id: str, resolved_model: str, endpoint: str, api_key: str,
                 transport_factory, parse_response, timeout_seconds: float):
        if (not isinstance(provider_id, str) or not provider_id.strip() or provider_id != provider_id.strip()
                or not isinstance(resolved_model, str) or not resolved_model.strip() or resolved_model != resolved_model.strip()
                or not isinstance(api_key, str) or not api_key.isascii() or not api_key.strip()
                or api_key != api_key.strip() or any(ord(char) <= 32 or ord(char) == 127 for char in api_key)
                or not callable(transport_factory) or not callable(parse_response)
                or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 3600
                or not math.isfinite(timeout_seconds)):
            raise ValueError("invalid guarded model configuration")
        _endpoint(endpoint)
        self._provider_id = provider_id
        self._resolved_model = resolved_model
        self._endpoint = endpoint
        self._api_key = api_key
        self._transport_factory = transport_factory
        self._parse_response = parse_response
        self._timeout = timeout_seconds

    async def __call__(self, *, model: str, message: str, rules: list, strategy: str = "model"):
        if model != self._provider_id:
            raise GuardedModelCallError("selected provider changed")
        request = build_openai_request(model=self._resolved_model, message=message, rules=rules, strategy=strategy)
        guard = SingleRequestGuard(endpoint=self._endpoint, expected=request)
        # This prototype supports standard OpenAI chat completions only. The
        # endpoint guard rejects query strings, credentials and ambiguous paths.
        base_url = self._endpoint.removesuffix("/chat/completions")
        import httpx
        from openai import APIStatusError, APITimeoutError, AsyncOpenAI

        delegate = None
        client = None
        try:
            delegate = self._transport_factory()
            transport = make_guarded_transport(delegate=delegate, guard=guard, api_key=self._api_key)
            http = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False, timeout=self._timeout)
            client = AsyncOpenAI(api_key=self._api_key, base_url=base_url, http_client=http,
                                 max_retries=0, timeout=self._timeout)
            completion = await client.chat.completions.create(**request)
            return await self._parse_response(completion, strategy)
        except asyncio.CancelledError:
            raise
        except (APITimeoutError, TimeoutError):
            raise TimeoutError("guarded model request timed out") from None
        except APIStatusError as error:
            if _is_context_limit_error(error):
                raise ContextLimitExceeded("context limit exceeded") from None
            raise GuardedModelCallError("guarded model request failed") from None
        except Exception:
            # SDK exceptions can contain request bodies, headers or credentials.
            raise GuardedModelCallError("guarded model request failed") from None
        finally:
            try:
                if client is not None:
                    await client.close()
                elif delegate is not None:
                    await delegate.aclose()
            except Exception:
                raise GuardedModelCallError("guarded model cleanup failed") from None
