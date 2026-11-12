import unittest
from types import SimpleNamespace

from sentinel.guarded_openai_invoker import GuardedModelCallError, GuardedOpenAIInvoker, _is_context_limit_error
from sentinel.openai_request_guard import RequestIntegrityError


class GuardedInvokerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.created = []
        def factory():
            self.created.append(True)
            self.fail("transport should not be created")
        async def parse(response, strategy):
            return response
        self.config = {"provider_id": "provider", "resolved_model": "model",
                       "endpoint": "https://example.invalid/v1/chat/completions", "api_key": "synthetic",
                       "transport_factory": factory, "parse_response": parse, "timeout_seconds": 10}
        self.rules = [{"rule_id": "r", "text": "rule", "allowed_actions": ["mute"],
                       "duration_mode": "strict", "severity_seconds": {"low": 10}}]

    async def test_changed_provider_is_rejected_before_transport(self):
        invoke = GuardedOpenAIInvoker(**self.config)
        with self.assertRaises(GuardedModelCallError):
            await invoke(model="other", message="private", rules=self.rules)
        self.assertEqual(self.created, [])

    async def test_context_limit_requires_explicit_structured_error_code(self):
        for body in ({"code": "context_length_exceeded"},
                     {"error": {"code": "model_context_window_exceeded"}},
                     {"code": "max_tokens_exceeded"}):
            self.assertTrue(_is_context_limit_error(SimpleNamespace(status_code=400, body=body)))
        for error in (SimpleNamespace(status_code=500, body={"code": "context_length_exceeded"}),
                      SimpleNamespace(status_code=400, body={"message": "context_length_exceeded"}),
                      SimpleNamespace(status_code=400, body={"code": ["context_length_exceeded"]}),
                      SimpleNamespace(status_code=400, body=None)):
            self.assertFalse(_is_context_limit_error(error))

    async def test_invalid_input_is_rejected_before_transport(self):
        invoke = GuardedOpenAIInvoker(**self.config)
        with self.assertRaises(RequestIntegrityError):
            await invoke(model="provider", message="private", rules=[])
        self.assertEqual(self.created, [])

    async def test_invalid_configuration_is_rejected_before_transport(self):
        for change in ({"provider_id": ""}, {"resolved_model": " model "}, {"api_key": " secret"},
                       {"api_key": "non ascii 密钥"}, {"timeout_seconds": True}, {"timeout_seconds": float("inf")},
                       {"timeout_seconds": 10**1000}, {"endpoint": "http://example.invalid/v1/chat/completions"}):
            with self.subTest(field=list(change)), self.assertRaises(ValueError):
                GuardedOpenAIInvoker(**{**self.config, **change})
        self.assertEqual(self.created, [])
