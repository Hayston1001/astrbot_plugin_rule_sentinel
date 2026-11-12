"""Experimental, fail-closed check for provider replacement or configuration changes.

Not wired into runtime. A binding does not prove provider behavior or endpoint
trust and cannot detect a change that is reverted between two checks.
"""

import hashlib
import hmac
import json
import secrets

from .dry_run_worker import Decision


MAX_PROVIDER_CONFIG_BYTES = 256 * 1024


def _digest(provider, provider_id, key):
    config = provider.provider_config
    model = provider.get_model()
    if (not isinstance(config, dict) or config.get("id") != provider_id
            or not isinstance(config.get("type"), str) or not config["type"].strip()
            or not isinstance(model, str) or not model.strip()):
        raise ValueError("invalid provider state")
    try:
        data = json.dumps({"config": config, "model": model}, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValueError("invalid provider state") from None
    if len(data) > MAX_PROVIDER_CONFIG_BYTES:
        raise ValueError("invalid provider state")
    return hmac.new(key, data, hashlib.sha256).digest()


class ProviderBinding:
    def __init__(self, resolver, provider_id, provider, key, digest):
        self._resolver = resolver
        self._provider_id = provider_id
        self._provider = provider
        self._key = key
        self._digest = digest

    @classmethod
    async def capture(cls, resolver, provider_id):
        if not callable(resolver) or not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("invalid provider identity")
        try:
            provider = await resolver(provider_id)
            key = secrets.token_bytes(32)
            digest = _digest(provider, provider_id, key)
        except Exception:
            raise ValueError("provider state unavailable") from None
        return cls(resolver, provider_id, provider, key, digest)

    async def is_current(self):
        try:
            provider = await self._resolver(self._provider_id)
            return (provider is self._provider
                    and hmac.compare_digest(self._digest, _digest(provider, self._provider_id, self._key)))
        except Exception:
            return False


class ProviderBindingGuard:
    """Recheck before and after a decision, without retrying or taking action."""

    def __init__(self, decide, binding):
        self.decide = decide
        self.binding = binding

    async def _current(self):
        try:
            return await self.binding.is_current() is True
        except Exception:
            return False

    async def __call__(self, item):
        if not await self._current():
            return Decision("skipped", "provider_binding_changed", model_attempted=False)
        result = await self.decide(item)
        if not isinstance(result, Decision) or result.status not in ("proposed", "no_action"):
            return result
        if await self._current():
            return result
        try:
            prior = json.loads(result.detail)
            if not isinstance(prior, dict):
                prior = {}
        except (TypeError, ValueError):
            prior = {}
        return Decision("review", json.dumps({"review_reason": "provider_binding_changed",
                                               "original_status": result.status, "decision": prior},
                                              ensure_ascii=False), model_attempted=result.model_attempted)
