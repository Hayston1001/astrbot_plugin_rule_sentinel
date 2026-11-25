"""Bounded, privacy-preserving administrator failure notifications.

The host supplies explicitly configured private recipients, a send function,
and audit callback. No recipient is selected automatically. Notifications are
informational and never authorize, retry, or execute moderation actions.

Every private notice names the failure, its scope, its consequence and where to
look. Because it goes only to saved administrators, it carries the triggering
message and sender identity so the problem is clear. Group notices are separate
and never include message text by default. Repeated failures of the same kind
are coalesced by the configured interval and the notice reports how many were
merged.
"""

import asyncio
import math
import time
from dataclasses import dataclass, field, replace
from datetime import datetime

from .async_io import run_sync_safely


@dataclass(frozen=True)
class NoticeSpec:
    label: str
    impact: str
    where: str


NOTICE_SPECS = {
    "model_failed": NoticeSpec(
        label="模型判定失败",
        impact="这条消息未完成判定, 不会执行任何动作, 也不会自动重试.",
        where="管理页 → 判定记录, 筛选失败结果.",
    ),
    "model_timeout": NoticeSpec(
        label="模型判定超时",
        impact="这条消息未完成判定, 不会执行任何动作, 也不会自动重试.",
        where="管理页 → 判定记录, 查看该条记录的处理时间.",
    ),
    "context_limit": NoticeSpec(
        label="判定输入超出模型限制",
        impact="这条消息没有送交模型, 不会执行任何动作.",
        where="管理页 → 判定记录, 确认输入来源; 群聊上下文条数可在设置里调小.",
    ),
    "rule_snapshot_too_large": NoticeSpec(
        label="规则快照超出记录上限",
        impact="这条消息没有开始判定, 不会执行任何动作.",
        where="管理页 → 群规则, 精简本群启用的规则.",
    ),
    "queue_full": NoticeSpec(
        label="群消息队列已满",
        impact="这条消息被跳过, 不会补判, 也不会执行任何动作.",
        where="设置 → 运行限制, 可提高每群队列容量.",
    ),
    "storage_failed": NoticeSpec(
        label="判定状态未能保存",
        impact="这条消息的处理结果可能已丢失, 需要人工核查.",
        where="管理页 → 总览 → 待复核, 并查看 AstrBot 日志.",
    ),
    "circuit_open": NoticeSpec(
        label="模型判定已熔断",
        impact="冷却期内的消息被跳过, 未判定, 不会执行任何动作.",
        where="管理页 → 判定记录, 查看连续失败原因.",
    ),
    "action_rejected": NoticeSpec(
        label="规则动作未执行",
        impact="目标属于管理员或机器人, 动作已按保护规则跳过.",
        where="无需处理; 如仍要处置请手动执行.",
    ),
}

# Failure details that are safe and useful to show. Anything else is omitted
# because free text could carry message content or provider details.
REASON_LABELS = {
    "decision_error": "判定过程出错",
    "decision_timeout": "等待模型响应超时",
    "invalid_decider_result": "判定结果格式无效",
    "invalid_decider_status": "判定结果状态无效",
    "audit_detail_invalid_or_too_large": "判定详情超出记录上限",
    "context_length_exceeded": "输入超出模型上下文",
    "evaluation_audit_unavailable": "判定记录存储不可用",
    "permission_denied": "机器人权限不足",
}

ACTION_LABELS = {"mute": "禁言", "recall": "撤回", "reply": "发言"}

MAX_NOTIFICATION_TEXT = 4000
MAX_SCOPE_FIELD = 128
MAX_GROUP_LABEL = 100
MAX_MESSAGE_PREVIEW = 2000
MAX_DURATION_SECONDS = 2592000
MAX_NOTIFICATION_TARGETS = 20
MAX_NOTIFICATION_TARGET_LENGTH = 512


def _message_preview(value) -> str:
    """One bounded, single-line excerpt of the triggering message."""
    if not isinstance(value, str):
        return ""
    text = "".join(char for char in value if char.isprintable() or char.isspace())
    text = " ".join(text.split())
    if len(text) > MAX_MESSAGE_PREVIEW:
        text = text[:MAX_MESSAGE_PREVIEW - 1].rstrip() + "…"
    return text


def _scope_field(value, *, name: str, limit: int = MAX_SCOPE_FIELD) -> str:
    if value is None:
        return ""
    if (not isinstance(value, str) or len(value) > limit or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(f"invalid notification {name}")
    return value


@dataclass(frozen=True)
class NotificationScope:
    """Where a failure happened. Identifiers plus a bounded message excerpt."""

    platform_id: str = ""
    group_id: str = ""
    message_id: str = ""
    rule_id: str = ""
    action: str = ""
    duration_seconds: int | None = None
    group_label: str = ""
    sender_id: str = ""
    message_text: str = ""

    def __post_init__(self):
        for name in ("platform_id", "group_id", "message_id", "rule_id", "sender_id"):
            object.__setattr__(self, name, _scope_field(getattr(self, name), name=name))
        object.__setattr__(self, "group_label", _scope_field(self.group_label, name="group label",
                                                           limit=MAX_GROUP_LABEL))
        object.__setattr__(self, "message_text", _message_preview(self.message_text))
        action = "" if self.action is None else self.action
        if action and action not in ACTION_LABELS:
            raise ValueError("invalid notification action")
        object.__setattr__(self, "action", action)
        if self.duration_seconds is not None and (type(self.duration_seconds) is not int
                or not 0 < self.duration_seconds <= MAX_DURATION_SECONDS):
            raise ValueError("invalid notification duration")


def validate_notification_targets(targets):
    if not isinstance(targets, (list, tuple)) or len(targets) > MAX_NOTIFICATION_TARGETS:
        raise ValueError("invalid notification recipient list")
    seen = set()
    for target in targets:
        if (not isinstance(target, str) or not target or len(target) > MAX_NOTIFICATION_TARGET_LENGTH
                or target != target.strip() or any(ord(char) < 32 or ord(char) == 127 for char in target)):
            raise ValueError("invalid notification recipient")
        parts = target.split(":", 2)
        if (len(parts) != 3 or not parts[0] or any(char.isspace() for char in parts[0])
                or parts[1] != "FriendMessage" or not parts[2] or parts[2] != parts[2].strip()):
            raise ValueError("recipient must be a private AstrBot session")
        if target in seen:
            raise ValueError("duplicate notification recipient")
        seen.add(target)
    return tuple(targets)


def notification_scope(*, platform_id="", group_id="", message_id="", rule_id="", action="",
                       duration_seconds=None, group_label="", sender_id="", message_text="") -> NotificationScope:
    """Build a scope from platform-provided identifiers.

    A notice must never be the reason a worker fails, so a value the scope
    cannot represent is left out instead of raising. Missing fields only make
    the message less specific.
    """
    fields = {}
    for name, value, limit in (("platform_id", platform_id, MAX_SCOPE_FIELD),
                               ("group_id", group_id, MAX_SCOPE_FIELD),
                               ("message_id", message_id, MAX_SCOPE_FIELD),
                               ("rule_id", rule_id, MAX_SCOPE_FIELD),
                               ("sender_id", sender_id, MAX_SCOPE_FIELD),
                               ("group_label", group_label, MAX_GROUP_LABEL)):
        try:
            fields[name] = _scope_field(value, name=name, limit=limit)
        except ValueError:
            fields[name] = ""
    if action in ACTION_LABELS:
        fields["action"] = action
    if type(duration_seconds) is int and 0 < duration_seconds <= MAX_DURATION_SECONDS:
        fields["duration_seconds"] = duration_seconds
    fields["message_text"] = _message_preview(message_text)
    return NotificationScope(**fields)


def render_notice(spec: NoticeSpec, scope: NotificationScope, *, label: str, occurrences: int = 1,
                  group_count: int = 1, now=None) -> str:
    lines = ["Rule Sentinel 失败提示", f"情况: {label}"]
    if scope.group_id:
        group = scope.group_label or scope.group_id
        lines.append(f"范围: 群 {group} ({scope.platform_id})" if scope.platform_id else f"范围: 群 {group}")
    elif scope.platform_id:
        lines.append(f"范围: 实例 {scope.platform_id}")
    if scope.sender_id:
        lines.append(f"发送者: {scope.sender_id}")
    if scope.message_id:
        lines.append(f"消息: {scope.message_id}")
    if scope.message_text:
        lines.append(f"消息原文: {scope.message_text}")
    if scope.rule_id:
        lines.append(f"规则: {scope.rule_id}")
    if scope.action:
        action = ACTION_LABELS[scope.action]
        if scope.action == "mute" and scope.duration_seconds:
            action = f"{action} {scope.duration_seconds} 秒"
        lines.append(f"动作: {action}")
    if now is not None:
        lines.append(f"时间: {now.strftime('%m-%d %H:%M')}")
    lines.append(f"影响: {spec.impact}")
    if occurrences > 1:
        merged = f"合并: 自上次同类提示以来 {occurrences} 次"
        if group_count > 1:
            merged += f", 涉及 {group_count} 个群"
        lines.append(merged)
    lines.append(f"去处: {spec.where}")
    return "\n".join(lines)[:MAX_NOTIFICATION_TEXT]


@dataclass
class _PendingNotice:
    count: int = 0
    groups: set = field(default_factory=set)
    scope: NotificationScope = field(default_factory=NotificationScope)
    reason: str = ""


class NotificationService:
    def __init__(self, *, recipients, interval_seconds: float, send, audit, clock=time.monotonic,
                 send_timeout_seconds: float = 10, wall_clock=datetime.now, resolve_group_label=None,
                 resolve_message_text=None):
        self.recipients = validate_notification_targets(recipients)
        if not self.recipients:
            raise ValueError("explicit private notification recipient required")
        if type(interval_seconds) not in (int, float) or not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("invalid notification interval")
        if type(send_timeout_seconds) not in (int, float) or not math.isfinite(send_timeout_seconds) or send_timeout_seconds <= 0:
            raise ValueError("invalid notification timeout")
        self.interval = interval_seconds
        self.send = send
        self.audit = audit
        self.clock = clock
        self.wall_clock = wall_clock
        self.resolve_group_label = resolve_group_label
        self.resolve_message_text = resolve_message_text
        self.send_timeout = send_timeout_seconds
        self.next_allowed = {}
        self.pending = {}
        self.lock = asyncio.Lock()

    async def notify(self, code: str, scope=None, reason: str = "") -> str:
        spec = NOTICE_SPECS.get(code)
        if spec is None:
            raise ValueError("unknown notification code")
        if scope is None:
            scope = NotificationScope()
        elif not isinstance(scope, NotificationScope):
            raise ValueError("invalid notification scope")
        if not isinstance(reason, str):
            raise ValueError("invalid notification reason")
        async with self.lock:
            now = self.clock()
            pending = self.pending.setdefault(code, _PendingNotice())
            pending.count += 1
            if scope.group_id:
                pending.groups.add((scope.platform_id, scope.group_id))
            pending.scope = scope
            pending.reason = reason
            if now < self.next_allowed.get(code, 0):
                return "rate_limited"
            # Reserve before the network call to prevent concurrent bursts and
            # repeated sends after ambiguous failures.
            self.next_allowed[code] = now + self.interval
            occurrences = pending.count
            group_count = len(pending.groups)
            pending.count = 0
            pending.groups = set()
            current = pending.scope
            current_reason = pending.reason
        label = spec.label
        if current_reason in REASON_LABELS:
            label = f"{spec.label}, {REASON_LABELS[current_reason]}"
        if current.group_id and not current.group_label and self.resolve_group_label is not None:
            try:
                resolved = await self.resolve_group_label(current)
            except Exception:
                resolved = ""
            if isinstance(resolved, str) and resolved:
                current = replace(current, group_label=resolved)
        if current.message_id and not current.message_text and self.resolve_message_text is not None:
            try:
                resolved = await self.resolve_message_text(current)
            except Exception:
                resolved = ""
            if isinstance(resolved, str) and resolved:
                current = replace(current, message_text=resolved)
        text = render_notice(spec, current, label=label, occurrences=occurrences,
                             group_count=group_count, now=self.wall_clock())
        statuses = set()
        for offset in range(0, len(self.recipients), 4):
            batch = self.recipients[offset:offset + 4]
            results = await asyncio.gather(*(self._send_one(target, code, text) for target in batch))
            statuses.update(results)
        if "audit_failed" in statuses:
            return "audit_failed"
        return next(iter(statuses)) if len(statuses) == 1 else "partial"

    async def _send_one(self, target, code, text):
        try:
            accepted = await asyncio.wait_for(self.send(target, text), timeout=self.send_timeout)
            # AstrBot's boolean only indicates that a platform was found. It is
            # not a receipt proving the administrator received the message.
            result = "submitted" if accepted is True else "failed" if accepted is False else "unknown"
        except Exception:
            # The transport may fail after a request was sent; delivery cannot
            # be inferred from a timeout or an exception.
            result = "unknown"
        try:
            await self.audit(code, result, text)
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

    def offer(self, code: str, scope=None, reason: str = "") -> bool:
        if code not in NOTICE_SPECS or self.task is None or self.closed:
            return False
        if scope is not None and not isinstance(scope, NotificationScope):
            return False
        if not isinstance(reason, str):
            return False
        if self.task.done():
            self.dropped += 1
            return False
        try:
            self.queue.put_nowait((code, scope, reason))
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            return False

    async def _run(self):
        while True:
            item = await self.queue.get()
            try:
                if item is None:
                    return
                code, scope, reason = item
                if scope is None and not reason:
                    status = await self.service.notify(code)
                else:
                    status = await self.service.notify(code, scope, reason)
                if status == "audit_failed":
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


TEST_MESSAGE = "Rule Sentinel 测试通知: 失败提示通道可用. 无需操作."


async def send_test_notifications(*, context, targets, message_chain_factory=None, send_timeout_seconds=10):
    """Send one test message to each saved recipient without retrying.

    Only explicitly saved private sessions are accepted. A transport exception
    leaves the result unknown because the request may already have been sent.
    """
    recipients = validate_notification_targets(targets)
    if not recipients:
        raise ValueError("explicit private notification recipient required")
    if type(send_timeout_seconds) not in (int, float) or not math.isfinite(send_timeout_seconds) \
            or send_timeout_seconds <= 0:
        raise ValueError("invalid notification timeout")
    if message_chain_factory is None:
        def message_chain_factory(text):
            from astrbot.api.event import MessageChain
            return MessageChain().message(text)

    async def send_one(target):
        try:
            accepted = await asyncio.wait_for(
                context.send_message(target, message_chain_factory(TEST_MESSAGE)),
                timeout=send_timeout_seconds)
            return "submitted" if accepted is True else "failed" if accepted is False else "unknown"
        except Exception:
            return "unknown"

    results = []
    for offset in range(0, len(recipients), 4):
        results.extend(await asyncio.gather(*(send_one(target)
                                             for target in recipients[offset:offset + 4])))
    return results


def build_notification_dispatcher(*, context, queue, recipients, interval_seconds: int,
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

    async def audit(code, status, summary):
        await run_sync_safely(queue.record_notification, code, status, summary)

    async def resolve_group_label(scope):
        return await run_sync_safely(queue.group_label, scope.platform_id, scope.group_id)

    async def resolve_message_text(scope):
        return await run_sync_safely(queue.message_content, platform_id=scope.platform_id,
                                     group_id=scope.group_id, message_id=scope.message_id)

    service = NotificationService(recipients=recipients, interval_seconds=interval_seconds,
                                  send=send, audit=audit, resolve_group_label=resolve_group_label,
                                  resolve_message_text=resolve_message_text)
    return NotificationDispatcher(service, capacity=64, on_failure=on_failure)
