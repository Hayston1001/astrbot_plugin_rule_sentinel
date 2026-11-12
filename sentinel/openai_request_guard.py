"""Experimental final-body check for a future isolated model transport.

This module is not wired into the runtime. Its caller must construct and freeze
the expected request independently, invoke the check at the actual transport
boundary, and prevent any later mutation. Matching a request cannot prove what
a remote service does with it or validate a model's output.
"""

import json
import hmac
import threading
from urllib.parse import urlsplit


MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024


class RequestIntegrityError(ValueError):
    """The final request differs from the independently prepared snapshot."""


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def _decode(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_REQUEST_BYTES:
        raise RequestIntegrityError("invalid request body")
    try:
        # The comparison must describe the bytes sent to an OpenAI-compatible
        # JSON endpoint, not rely on json.loads' UTF-16/UTF-32 autodetection.
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates,
                            parse_constant=_reject_constant)
        if not isinstance(result, dict):
            raise ValueError("request must be an object")
        return json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise RequestIntegrityError("invalid request body") from None


class RequestBodyGuard:
    """Compare a frozen expected JSON request with the final serialized body."""

    def __init__(self, expected: dict):
        if not isinstance(expected, dict):
            raise RequestIntegrityError("invalid expected request")
        model = expected.get("model")
        messages = expected.get("messages")
        tools = expected.get("tools")
        if (not isinstance(model, str) or not model.strip() or expected.get("stream") is not False
                or expected.get("tool_choice") != "auto" or not isinstance(messages, list) or len(messages) != 2
                or any(not isinstance(message, dict) or set(message) != {"role", "content"}
                       or message.get("role") != role or not isinstance(message.get("content"), str)
                       or not message["content"].strip()
                       for message, role in zip(messages, ("system", "user")))
                or not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict)
                or tools[0].get("type") != "function" or not isinstance(tools[0].get("function"), dict)
                or tools[0]["function"].get("name") not in ("propose_action", "assess_rules")):
            raise RequestIntegrityError("invalid expected request")
        try:
            raw = json.dumps(expected, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
            raise RequestIntegrityError("invalid expected request") from None
        self._expected = _decode(raw)

    def check(self, final_body: bytes) -> None:
        if _decode(final_body) != self._expected:
            # Do not include either request body in exceptions or logs.
            raise RequestIntegrityError("request body changed")


def _endpoint(url):
    if (not isinstance(url, str) or not url.isascii() or not 1 <= len(url) <= 2048
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or "?" in url or "#" in url or "\\" in url):
        raise RequestIntegrityError("invalid request endpoint")
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise RequestIntegrityError("invalid request endpoint") from None
    if (parts.scheme != "https" or not host or not host.isascii() or "%" in host
            or parts.username is not None or parts.password is not None
            or not parts.path.endswith("/chat/completions") or "%" in parts.path
            or any(segment in (".", "..") for segment in parts.path.split("/"))
            or "//" in parts.path or (port is not None and not 1 <= port <= 65535)):
        raise RequestIntegrityError("invalid request endpoint")
    return host.casefold(), port or 443, parts.path


class SingleRequestGuard:
    """Experimental single-attempt check for an independently selected endpoint.

    Consume before validation so an altered first attempt cannot be followed by
    a retry. Transport isolation, TLS, headers and redirects remain the caller's
    responsibility; this is not yet a production network boundary.
    """

    def __init__(self, *, endpoint: str, expected: dict):
        self._endpoint = _endpoint(endpoint)
        self._body = RequestBodyGuard(expected)
        self._lock = threading.Lock()
        self._consumed = False

    def check_and_consume(self, *, method: str, url: str, body: bytes) -> None:
        with self._lock:
            if self._consumed:
                raise RequestIntegrityError("request attempt already consumed")
            self._consumed = True
        if method != "POST" or _endpoint(url) != self._endpoint:
            raise RequestIntegrityError("request destination changed")
        self._body.check(body)

    def abort(self) -> None:
        """Prevent subsequent attempts after an unreadable or invalid request."""
        with self._lock:
            self._consumed = True


def make_guarded_transport(*, delegate, guard: SingleRequestGuard, api_key: str):
    """Build an isolated httpx transport prototype; never wrap a shared client.

    The caller owns the guard for one request and must use a dedicated client,
    HTTPS verification and appropriate proxy/DNS policy. This guard does not
    establish trust in the provider, address, expected body or server response.
    """
    import httpx

    if (not isinstance(guard, SingleRequestGuard) or not isinstance(api_key, str)
            or not api_key.isascii() or not api_key.strip() or api_key != api_key.strip()
            or any(ord(char) <= 32 or ord(char) == 127 for char in api_key)):
        raise RequestIntegrityError("invalid transport configuration")
    authorization = f"Bearer {api_key}"

    class BoundedResponseStream(httpx.AsyncByteStream):
        def __init__(self, source):
            self.source = source
            self.closed = False

        async def __aiter__(self):
            size = 0
            try:
                async for chunk in self.source:
                    if not isinstance(chunk, bytes):
                        raise RequestIntegrityError("invalid response stream")
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise RequestIntegrityError("response too large")
                    yield chunk
            finally:
                await self.aclose()

        async def aclose(self):
            if not self.closed:
                self.closed = True
                await self.source.aclose()

    class GuardedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            try:
                body = request.content
            except httpx.RequestNotRead:
                guard.abort()
                raise RequestIntegrityError("streaming request body not supported") from None
            guard.check_and_consume(method=request.method, url=str(request.url), body=body)
            auth = request.headers.get_list("authorization")
            hosts = request.headers.get_list("host")
            content_types = request.headers.get_list("content-type")
            content_lengths = request.headers.get_list("content-length")
            routing_headers = ("forwarded", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port",
                               "x-forwarded-prefix", "x-original-url", "x-rewrite-url",
                               "x-http-method-override", "x-method-override", "x-http-method")
            if (len(auth) != 1 or not auth[0].isascii() or not hmac.compare_digest(auth[0], authorization)
                    or len(hosts) != 1 or len(content_types) != 1
                    or content_types[0].lower().strip() != "application/json"
                    or len(content_lengths) != 1 or content_lengths[0] != str(len(body))
                    or request.headers.get_list("cookie") or request.headers.get_list("proxy-authorization")
                    or request.headers.get_list("content-encoding") or request.headers.get_list("transfer-encoding")
                    or any(request.headers.get_list(name) for name in routing_headers)
                    or "sni_hostname" in request.extensions):
                raise RequestIntegrityError("invalid request headers")
            path = urlsplit(str(request.url)).path
            if _endpoint(f"https://{hosts[0]}{path}") != guard._endpoint:
                raise RequestIntegrityError("request host changed")
            # Avoid automatically decompressing an unbounded response. Servers
            # that ignore this and send compressed content are rejected below.
            request.headers["accept-encoding"] = "identity"
            response = await delegate.handle_async_request(request)
            if 300 <= response.status_code < 400:
                await response.aclose()
                raise RequestIntegrityError("redirect not allowed")
            encodings = response.headers.get_list("content-encoding")
            lengths = response.headers.get_list("content-length")
            if (len(encodings) > 1 or (encodings and encodings[0].strip().lower() != "identity")
                    or len(lengths) > 1 or (lengths and (len(lengths[0]) > 16 or not lengths[0].isascii() or not lengths[0].isdigit()
                                                         or int(lengths[0]) > MAX_RESPONSE_BYTES))):
                await response.aclose()
                raise RequestIntegrityError("invalid or oversized response")
            if hasattr(response, "_content"):
                if len(response.content) > MAX_RESPONSE_BYTES:
                    await response.aclose()
                    raise RequestIntegrityError("response too large")
            else:
                response.stream = BoundedResponseStream(response.stream)
            return response

        async def aclose(self):
            await delegate.aclose()

    return GuardedTransport()
