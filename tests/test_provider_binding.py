import asyncio
import json
import unittest
from types import SimpleNamespace

from sentinel.dry_run_worker import Decision
from sentinel.provider_binding import ProviderBinding, ProviderBindingGuard


class Provider:
    def __init__(self):
        self.provider_config = {"id": "p", "type": "openai", "api_base": "https://example.invalid",
                                "key": ["secret"], "custom_extra_body": {}}
        self.model = "model"

    def get_model(self):
        return self.model


class ProviderBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = Provider()
        async def resolver(provider_id):
            self.assertEqual(provider_id, "p")
            return self.provider
        self.resolver = resolver
        self.binding = await ProviderBinding.capture(resolver, "p")

    async def test_model_endpoint_key_and_nested_settings_changes_are_detected(self):
        self.assertTrue(await self.binding.is_current())
        for field, value in (("api_base", "https://other.invalid"), ("key", ["other-secret"]),
                             ("custom_extra_body", {"tools": []})):
            with self.subTest(field=field):
                previous = self.provider.provider_config[field]
                self.provider.provider_config[field] = value
                self.assertFalse(await self.binding.is_current())
                self.provider.provider_config[field] = previous
        self.provider.model = "other"
        self.assertFalse(await self.binding.is_current())

    async def test_replaced_provider_and_resolver_failure_deny(self):
        self.provider = Provider()
        self.assertFalse(await self.binding.is_current())
        async def fail(provider_id):
            raise RuntimeError("private secret")
        self.binding._resolver = fail
        self.assertFalse(await self.binding.is_current())

    async def test_invalid_config_fails_without_exposing_credentials(self):
        self.provider.provider_config["extra"] = object()
        with self.assertRaises(ValueError) as raised:
            await ProviderBinding.capture(self.resolver, "p")
        self.assertNotIn("secret", str(raised.exception))

    async def test_changed_before_decision_is_skipped_without_model_call(self):
        self.provider.model = "changed"
        async def decide(item):
            self.fail("changed provider reached model")
        result = await ProviderBindingGuard(decide, self.binding)(SimpleNamespace())
        self.assertEqual((result.status, result.detail, result.model_attempted),
                         ("skipped", "provider_binding_changed", False))

    async def test_change_during_decision_requires_review_without_losing_result(self):
        async def decide(item):
            self.provider.provider_config["key"] = ["other-secret"]
            return Decision("proposed", json.dumps({"proposal": {"rule_id": "r"}}))
        result = await ProviderBindingGuard(decide, self.binding)(SimpleNamespace())
        self.assertEqual(result.status, "review")
        self.assertEqual(json.loads(result.detail)["review_reason"], "provider_binding_changed")
        self.assertEqual(json.loads(result.detail)["decision"]["proposal"]["rule_id"], "r")
        self.assertNotIn("secret", result.detail)

    async def test_resolver_cancellation_is_not_suppressed(self):
        async def cancelled(provider_id):
            raise asyncio.CancelledError()
        self.binding._resolver = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.binding.is_current()
