"""Bounded, privacy-preserving administrator fault notifications.

The host supplies an explicitly configured private recipient, send function,
and audit callback. No recipient is selected automatically. Notifications are
informational and never authorize, retry, or execute moderation actions.
"""

import asyncio
import math
import time

from .async_io import run_sync_safely


MESSAGES = {
    "model_failed": "规则监控模型请求失败. 请查看插件审计.",
    "model_timeout": "规则监控模型请求超时. 请查看插件审计.",
    "context_limit": "规则监控输入超出模型限制. 请查看插件审计.",
    "rule_snapshot_too_large": "规则快照超出审计记录大小限制, 判定未启动. 请查看插件审计.",
    "queue_full": "规则监控队列已满, 有消息未处理. 请查看插件审计.",
    "storage_failed": "规则监控存储失败. 请查看插件日志.",
    "circuit_open": "规则监控模型熔断, 有消息未处理. 请查看插件审计.",
}


class NotificationService:
    def __init__(self, *, recipient: str, interval_seconds: float, send, audit, clock=time.monotonic,
                 send_timeout_seconds: float = 10):
        if not isinstance(recipient, str) or not recipient.strip():
            raise ValueError("explicit private notification recipient required")
        parts = recipient.split(":", 2)
        if len(parts) != 3 or not parts[0].strip() or parts[1] != "FriendMessage" or not parts[2].strip():
            raise ValueError("recipient must be a private AstrBot session")
        if type(interval_seconds) not in (int, float) or not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("invalid notification interval")
        if type(send_timeout_seconds) not in (int, float) or not math.isfinite(send_timeout_seconds) or send_timeout_seconds <= 0:
            raise ValueError("invalid notification timeout")
        self.recipient = recipient
        self.interval = interval_seconds
        self.send = send
        self.audit = audit
        self.clock = clock
        self.send_timeout = send_timeout_seconds
        self.next_allowed = {}
        self.lock = asyncio.Lock()

    async def notify(self, code: str) -> str:
        if code not in MESSAGES:
            raise ValueError("unknown notification code")
        async with self.lock:
            now = self.clock()
            if now < self.next_allowed.get(code, 0):
                return "rate_limited"
            # Reserve before the network call to prevent concurrent bursts and
            # repeated sends after ambiguous failures.
            self.next_allowed[code] = now + self.interval
        try:
            accepted = await asyncio.wait_for(self.send(self.recipient, MESSAGES[code]), timeout=self.send_timeout)
            # AstrBot's boolean only indicates that a platform was found. It is
            # not a receipt proving the administrator received the message.
            result = "submitted" if accepted is True else "failed" if accepted is False else "unknown"
        except Exception:
            # The transport may fail after a request was sent; delivery cannot
            # be inferred from a timeout or an exception.
            result = "unknown"
        try:
            await self.audit(code, result)
        except Exception:
            # Callers must also log audit failures; no message contents or
            # provider exceptions are returned to the ordinary chat flow.
            return "audit_failed"
        return result


class NotificationDispatcher:
    """Single background sender with an explicitly bounded in-memory buffer."""

    def __init__(self, service, *, capacity: int, on_failure=None):
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("invalid notification buffer capacity")
        self.service = service
        self.queue = asyncio.Queue(maxsize=capacity)
        self.dropped = 0
        self.audit_failures = 0
        self.task = None
        self.closed = False
        self.failures = 0
        self.on_failure = on_failure
        self._close_task = None

    def start(self):
        if self.task is not None or self.closed:
            raise RuntimeError("notification dispatcher already started or closed")
        self.task = asyncio.create_task(self._run())
        self.task.add_done_callback(self._consumer_done)

    def _consumer_done(self, task):
        if task.cancelled() or task.exception() is not None or not self.closed:
            self.failures += 1
            if self.on_failure is not None:
                try:
                    self.on_failure("notification_consumer_failed")
                except Exception:
                    pass

    def offer(self, code: str) -> bool:
        if code not in MESSAGES or self.task is None or self.closed:
            return False
        if self.task.done():
            self.dropped += 1
            return False
        try:
            self.queue.put_nowait(code)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            return False

    async def _run(self):
        while True:
            code = await self.queue.get()
            try:
                if code is None:
                    return
                if await self.service.notify(code) == "audit_failed":
                    self.audit_failures += 1
            except Exception:
                self.audit_failures += 1
            finally:
                self.queue.task_done()

    async def close(self):
        if self._close_task is None:
            self.closed = True
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        if self.task is None:
            return
        if not self.task.done():
            sentinel = asyncio.create_task(self.queue.put(None))
            done, _ = await asyncio.wait((self.task, sentinel), return_when=asyncio.FIRST_COMPLETED)
            if sentinel not in done:
                sentinel.cancel()
                await asyncio.gather(sentinel, return_exceptions=True)
        await asyncio.gather(self.task, return_exceptions=True)
        while not self.queue.empty():
            item = self.queue.get_nowait()
            self.queue.task_done()
            if item is not None:
                self.dropped += 1


def build_notification_dispatcher(*, context, queue, recipient: str, interval_seconds: int,
                                  message_chain_factory=None, is_authorized=lambda: True, on_failure=None):
    """Build but do not start a dispatcher using AstrBot's proactive API."""
    if message_chain_factory is None:
        def message_chain_factory(text):
            from astrbot.api.event import MessageChain
            return MessageChain().message(text)

    async def send(target, text):
        if is_authorized() is not True:
            return False
        return await context.send_message(target, message_chain_factory(text))

    async def audit(code, status):
        await run_sync_safely(queue.record_notification, code, status)

    service = NotificationService(recipient=recipient, interval_seconds=interval_seconds,
                                  send=send, audit=audit)
    return NotificationDispatcher(service, capacity=64, on_failure=on_failure)
