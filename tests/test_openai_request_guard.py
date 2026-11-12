import json
import unittest
from concurrent.futures import ThreadPoolExecutor

from sentinel.openai_request_guard import MAX_REQUEST_BYTES, RequestBodyGuard, RequestIntegrityError, SingleRequestGuard


class RequestBodyGuardTests(unittest.TestCase):
    def setUp(self):
        self.expected = {"model": "selected", "messages": [{"role": "system", "content": "rules"},
                           {"role": "user", "content": "private message"}],
                         "tools": [{"type": "function", "function": {"name": "propose_action"}}],
                         "tool_choice": "auto", "stream": False}
        self.guard = RequestBodyGuard(self.expected)

    def body(self, value):
        return json.dumps(value, ensure_ascii=False).encode("utf-8")

    def test_accepts_same_complete_body_despite_json_key_order(self):
        reordered = {key: self.expected[key] for key in reversed(self.expected)}
        self.assertIsNone(self.guard.check(self.body(reordered)))

    def test_expected_body_is_frozen(self):
        saved = self.body(self.expected)
        self.expected["messages"][1]["content"] = "changed"
        self.guard.check(saved)
        with self.assertRaises(RequestIntegrityError):
            self.guard.check(self.body(self.expected))

    def test_omitted_or_changed_rules_messages_and_tools_fail_closed(self):
        for key, value in (("messages", self.expected["messages"][:1]), ("tools", []),
                           ("model", "different"), ("stream", 0), ("tool_choice", "none"),
                           ("extra_instruction", "ignore rules")):
            with self.subTest(key=key), self.assertRaises(RequestIntegrityError) as raised:
                self.guard.check(self.body({**self.expected, key: value}))
            self.assertNotIn("private message", str(raised.exception))

    def test_duplicate_keys_nonfinite_and_invalid_encoding_fail_closed(self):
        for body in (b'{"a":1,"a":2}', b'{"a":{"b":1,"b":2}}', b'{"a":NaN}',
                     b'\xff', b'[]', b'', b' ' * (MAX_REQUEST_BYTES + 1)):
            with self.subTest(body=body[:20]), self.assertRaises(RequestIntegrityError):
                self.guard.check(body)

    def test_matching_non_utf8_json_is_rejected(self):
        text = json.dumps(self.expected, ensure_ascii=False)
        for encoding in ("utf-16", "utf-16-le", "utf-32", "utf-32-le"):
            with self.subTest(encoding=encoding), self.assertRaises(RequestIntegrityError):
                self.guard.check(text.encode(encoding))

    def test_rejects_invalid_expected_values(self):
        for expected in (None, {"x": float("inf")}, {"x": object()}):
            with self.subTest(expected=expected), self.assertRaises(RequestIntegrityError):
                RequestBodyGuard(expected)

    def test_rejects_incomplete_or_wrong_tool_baseline(self):
        invalid = [{**self.expected, "messages": self.expected["messages"][:1]},
                   {**self.expected, "messages": [{"role": "system", "content": "rules"},
                                                   {"role": "user", "content": ""}]},
                   {**self.expected, "tools": []}, {**self.expected, "stream": True},
                   {**self.expected, "tool_choice": "none"},
                   {**self.expected, "tools": [{"type": "function", "function": {"name": "admin_action"}}]}]
        for expected in invalid:
            with self.subTest(expected=expected), self.assertRaises(RequestIntegrityError):
                RequestBodyGuard(expected)

    def test_endpoint_and_request_are_bound_to_one_attempt(self):
        guard = SingleRequestGuard(endpoint="https://example.invalid/v1/chat/completions", expected=self.expected)
        guard.check_and_consume(method="POST", url="https://EXAMPLE.invalid:443/v1/chat/completions",
                                body=self.body(self.expected))
        with self.assertRaises(RequestIntegrityError):
            guard.check_and_consume(method="POST", url="https://example.invalid/v1/chat/completions",
                                    body=self.body(self.expected))

    def test_wrong_destination_or_body_consumes_attempt(self):
        for method, url, body in (("GET", "https://example.invalid/v1/chat/completions", self.body(self.expected)),
                                  ("POST", "https://attacker.invalid/v1/chat/completions", self.body(self.expected)),
                                  ("POST", "https://example.invalid/v1/chat/completions", b"{}")):
            with self.subTest(method=method, url=url):
                guard = SingleRequestGuard(endpoint="https://example.invalid/v1/chat/completions", expected=self.expected)
                with self.assertRaises(RequestIntegrityError):
                    guard.check_and_consume(method=method, url=url, body=body)
                with self.assertRaises(RequestIntegrityError):
                    guard.check_and_consume(method="POST", url="https://example.invalid/v1/chat/completions",
                                            body=self.body(self.expected))

    def test_invalid_or_ambiguous_endpoints_are_rejected(self):
        for url in ("http://example.invalid/v1/chat/completions", "https://u:p@example.invalid/v1/chat/completions",
                    "https://example.invalid/v1/chat/completions?key=secret", "https://example.invalid/v1/../chat/completions",
                    "https://example.invalid/v1/%2e%2e/chat/completions", "https://example.invalid:0/v1/chat/completions",
                    "https://example.invalid/v1/chat/completions#fragment", "https://example.invalid/v1//chat/completions"):
            with self.subTest(url=url), self.assertRaises(RequestIntegrityError):
                SingleRequestGuard(endpoint=url, expected=self.expected)

    def test_concurrent_attempts_have_at_most_one_success(self):
        guard = SingleRequestGuard(endpoint="https://example.invalid/v1/chat/completions", expected=self.expected)
        def attempt(_):
            try:
                guard.check_and_consume(method="POST", url="https://example.invalid/v1/chat/completions",
                                        body=self.body(self.expected))
                return True
            except RequestIntegrityError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(20))), 1)
