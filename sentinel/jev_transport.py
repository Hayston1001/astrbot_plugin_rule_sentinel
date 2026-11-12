"""Minimal Jev HTTPS transport using an injected aiohttp-compatible session."""

import asyncio
import json

from .decision_errors import ContextLimitExceeded


class JevRequestError(Exception):
    """A safe-to-log failure without response contents or credentials."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("invalid JSON constant")


def _parse_json(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def _header_values(headers, name):
    getall = getattr(headers, "getall", None)
    if callable(getall):
        return list(getall(name, []))
    value = headers.get(name)
    return [] if value is None else [value]


class JevTransport:
    ENDPOINT = "https://api.typesafe.ai/v1/systemone"
    ENDPOINTS = {"typesafe": ENDPOINT, "openrouter": "https://openrouter.ai/api/v1/systemone"}
    MAX_RESPONSE_BYTES = 1024 * 1024

    def __init__(self, session, *, api_key: str, timeout_seconds: float, provider="typesafe"):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Jev API key is required")
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds < float("inf"):
            raise ValueError("invalid request timeout")
        if not isinstance(provider, str) or provider not in self.ENDPOINTS:
            raise ValueError("invalid Jev provider")
        self._session = session
        self._api_key = api_key
        self._timeout = timeout_seconds
        self.ENDPOINT = self.ENDPOINT if provider == "typesafe" else self.ENDPOINTS[provider]

    async def __call__(self, request: dict) -> dict:
        if not isinstance(request, dict) or not isinstance(request.get("model"), str) or not request["model"].strip():
            raise ValueError("explicit model is required")
        try:
            async with asyncio.timeout(self._timeout):
                async with self._session.post(
                    self.ENDPOINT, json=request,
                    headers={"Authorization": f"Bearer {self._api_key}", "Accept-Encoding": "identity"},
                    allow_redirects=False,
                    auto_decompress=False,
                ) as response:
                    encodings = _header_values(response.headers, "Content-Encoding")
                    lengths = _header_values(response.headers, "Content-Length")
                    if (len(encodings) > 1 or (encodings and encodings[0].strip().lower() != "identity")
                            or len(lengths) > 1 or (lengths and (len(lengths[0]) > 16
                                or not lengths[0].isascii() or not lengths[0].isdigit()
                                or int(lengths[0]) > self.MAX_RESPONSE_BYTES))):
                        raise JevRequestError("invalid or oversized Jev response")
                    chunks = []
                    size = 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > self.MAX_RESPONSE_BYTES:
                            raise JevRequestError("Jev response too large")
                        chunks.append(chunk)
                    if response.status != 200:
                        if response.status == 400:
                            try:
                                error = _parse_json(b"".join(chunks))
                                detail = error.get("detail") if isinstance(error, dict) else None
                                code = detail.get("code") if isinstance(detail, dict) else None
                                if code == "max_tokens_exceeded" or detail == "max_tokens_exceeded" or (isinstance(error, dict) and error.get("code") == "max_tokens_exceeded"):
                                    raise ContextLimitExceeded("context limit exceeded")
                            except (ValueError, UnicodeDecodeError):
                                pass
                        raise JevRequestError(f"Jev HTTP status {response.status}")
                    parsed = _parse_json(b"".join(chunks))
                    if not isinstance(parsed, dict):
                        raise JevRequestError("invalid Jev response")
                    return parsed
        except (TimeoutError, JevRequestError, ContextLimitExceeded):
            raise
        except Exception:
            raise JevRequestError("Jev request or response failed") from None
