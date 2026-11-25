"""Admission of text snapshots into the persistent dry-run queue."""

import time
from dataclasses import dataclass, field
from types import SimpleNamespace

from .async_io import run_sync_safely
from .monitoring_policy import eligibility
from .notifications import notification_scope


MAX_MESSAGE_CHARS = 65536


@dataclass(frozen=True)
class IncomingMessage:
    platform_id: str
    group_id: str
    message_id: str
    sender_id: str
    bot_id: str | None
    text: str
    is_text: bool
    is_management_command: bool
    sender_is_verified_non_bot: bool = False
    session_origin: str | None = None
    message_time: str | None = None
    original_text: str | None = None
    image_urls: tuple[str, ...] = ()
    received_at: float = field(default_factory=time.monotonic)
    sender_display_name: str | None = None


class DryRunAdmission:
    """Admit group-scoped messages after the applicable policy checks."""

    def __init__(self, queue, rules, *, policy, capacity: int, model: str, rate_limiter=None,
                 settings_match=None, notify=None, session_gate=None, image_policy=None,
                 chat_context=None, context_policy=None):
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("queue capacity must be a positive integer")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be explicitly selected")
        self.queue = queue
        self.rules = rules
        self.policy = policy
        self.capacity = capacity
        self.model = model
        self.rate_limiter = rate_limiter
        self.settings_match = settings_match
        self.notify = notify
        self.session_gate = session_gate
        self.image_policy = image_policy
        self.chat_context = chat_context
        self.context_policy = context_policy

    async def submit(self, message: IncomingMessage) -> str:
        # Persist nothing when identity is insufficient for a group-scoped record.
        if self.settings_match is not None and not self.settings_match():
            return "settings_changed_reload_required"
        if any(not isinstance(value, str) or not value.strip() for value in
               (message.platform_id, message.group_id, message.message_id)):
            return "invalid_message_identity"
        if self.session_gate is not None and await self.session_gate(message) is not True:
            return "framework_session_denied"
        rules = await run_sync_safely(self.rules.current_for_group, message.platform_id, message.group_id)
        text = message.text if isinstance(message.text, str) else ""
        images = tuple(message.image_urls) if isinstance(message.image_urls, (tuple, list)) else ()
        image_enabled, image_limit = False, 0
        if images and self.image_policy is not None:
            try:
                enabled, limit = self.image_policy(message.platform_id, message.group_id)
            except Exception:
                enabled, limit = False, 0
            image_enabled = enabled is True
            image_limit = limit if type(limit) is int and limit > 0 else 0
        status = eligibility(self.policy, platform_id=message.platform_id, group_id=message.group_id,
                             sender_id=message.sender_id, bot_id=message.bot_id,
                             sender_is_verified_non_bot=message.sender_is_verified_non_bot,
                             has_enabled_rule=any(rule.enabled for rule in rules),
                             is_management_command=message.is_management_command,
                             is_text=message.is_text, has_images=bool(images), image_enabled=image_enabled)
        if status == "eligible" and images and image_limit and len(images) > image_limit:
            status = "image_too_many"
        if status == "eligible" and (not text.strip() and not images):
            status = "empty_text"
        if status == "eligible" and len(text) > MAX_MESSAGE_CHARS:
            status = "message_too_long"
        if status == "eligible" and (message.original_text is not None and
                (not isinstance(message.original_text, str) or len(message.original_text) > MAX_MESSAGE_CHARS)):
            status = "message_too_long"
        if status == "eligible" and self.rate_limiter is not None:
            if not await self.rate_limiter.allow(message.platform_id, message.group_id):
                status = "rate_limited"
        if status != "eligible":
            await run_sync_safely(self.queue.record_skip, platform_id=message.platform_id,
                                    group_id=message.group_id, message_id=message.message_id,
                                    reason=status)
            return status
        if self.settings_match is not None and not self.settings_match():
            return "settings_changed_reload_required"
        payload = {"text": text, "sender_id": message.sender_id, "session_origin": message.session_origin}
        if images:
            payload["image_urls"] = list(images)
        if message.original_text is not None:
            payload["original_text"] = message.original_text
        if message.message_time is not None:
            payload["message_time"] = message.message_time
        if message.sender_display_name is not None:
            payload["sender_display_name"] = message.sender_display_name
        context_limit = self.context_policy(message.platform_id, message.group_id) if self.context_policy else 0
        token = None
        result = None
        try:
            if context_limit:
                token = self.chat_context.prepare(message, context_limit)
                payload["context_token"] = token
            result = await run_sync_safely(self.queue.enqueue, platform_id=message.platform_id,
                group_id=message.group_id, message_id=message.message_id, capacity=self.capacity,
                payload=payload)
        finally:
            if token is not None:
                if result == "queued":
                    self.chat_context.remember(message)
                else:
                    self.chat_context.discard(token)
        if result == "queue_full" and self.notify is not None:
            try:
                self.notify("queue_full", notification_scope(platform_id=message.platform_id,
                                                             group_id=message.group_id,
                                                             message_id=message.message_id))
            except Exception:
                pass
        return result

    async def record_external_skip(self, skipped) -> str:
        """Record a capture skip only after group and framework checks pass."""
        if self.settings_match is not None and not self.settings_match():
            return "settings_changed_reload_required"
        key = (skipped.platform_id, skipped.group_id)
        if self.policy.mode == "allowlist" and key not in self.policy.groups:
            return "group_not_allowed"
        if self.policy.mode == "blocklist" and key in self.policy.groups:
            return "group_blocked"
        if self.policy.mode not in ("allowlist", "blocklist"):
            return "invalid_list_mode"
        session = SimpleNamespace(platform_id=skipped.platform_id, group_id=skipped.group_id,
                                  session_origin=skipped.session_origin)
        # A caller reading events outside the framework pipeline must pass a
        # gate; the standard listener already passed it and leaves this None.
        if self.session_gate is not None and await self.session_gate(session) is not True:
            return "framework_session_denied"
        rules = await run_sync_safely(self.rules.current_for_group, skipped.platform_id, skipped.group_id)
        if not any(rule.enabled for rule in rules):
            return "no_enabled_rule"
        if self.settings_match is not None and not self.settings_match():
            return "settings_changed_reload_required"
        await run_sync_safely(self.queue.record_skip, platform_id=skipped.platform_id,
                                group_id=skipped.group_id, message_id=skipped.message_id,
                                reason=skipped.reason)
        return "skipped"
