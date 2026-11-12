"""Strict, bounded discovery of the supported Jev model."""

import asyncio
import re

from .jev_transport import JevRequestError, _header_values, _parse_json


MODELS_ENDPOINT = "https://api.typesafe.ai/v1/models"
OPENROUTER_MODEL = "typesafe/jev-latest"


async def list_jev_models(provider, api_key, *, session=None):
    if not isinstance(provider, str) or provider not in ("typesafe", "openrouter"):
        raise ValueError("invalid provider")
    if not isinstance(api_key, str) or len(api_key) > 4096 or "\n" in api_key or "\r" in api_key:
        raise ValueError("invalid key")
    if provider == "openrouter":
        if api_key and api_key in OPENROUTER_MODEL:
            raise ValueError("invalid key")
        return [OPENROUTER_MODEL]
    if not api_key.strip() or session is None:
        raise ValueError("missing key or session")
    try:
        async with asyncio.timeout(10):
            async with session.get(MODELS_ENDPOINT, headers={"Authorization": f"Bearer {api_key}",
                    "Accept-Encoding": "identity"}, allow_redirects=False, auto_decompress=False) as response:
                encodings = _header_values(response.headers, "Content-Encoding")
                lengths = _header_values(response.headers, "Content-Length")
                if (len(encodings) > 1 or (encodings and encodings[0].strip().lower() != "identity")
                        or len(lengths) > 1 or (lengths and (len(lengths[0]) > 16 or not lengths[0].isascii()
                            or not lengths[0].isdigit() or int(lengths[0]) > 1048576))):
                    raise JevRequestError("invalid model response")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 1048576:
                        raise JevRequestError("invalid model response")
                    chunks.append(chunk)
                if response.status != 200:
                    raise JevRequestError("model request failed")
                data = _parse_json(b"".join(chunks))
                if not isinstance(data, dict) or not isinstance(data.get("models"), list):
                    raise JevRequestError("invalid model response")
                names = []
                for item in data["models"]:
                    if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not re.fullmatch(
                            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item["name"]):
                        raise JevRequestError("invalid model response")
                    names.append(item["name"])
                if "jev-latest" not in names or api_key in "jev-latest":
                    raise JevRequestError("model unavailable")
                return ["jev-latest"]
    except (ValueError, TypeError, UnicodeDecodeError, TimeoutError, JevRequestError):
        raise JevRequestError("model request failed") from None
    except Exception:
        raise JevRequestError("model request failed") from None
