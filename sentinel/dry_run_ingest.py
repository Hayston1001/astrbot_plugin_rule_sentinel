"""Admission of text snapshots into the persistent dry-run queue."""

from dataclasses import dataclass
from types import SimpleNamespace

from .async_io import run_sync_safely
from .monitoring_policy import eligibility


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


class DryRunAdmission:
    """Admit group-scoped messages after the applicable policy checks."""

    def __init__(self, queue, rules, *, policy, capacity: int, model: str, rate_limiter=None,
                 settings_match=None, notify=None, session_gate=None):
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
        status = eligibility(self.policy, platform_id=message.platform_id, group_id=message.group_id,
                             sender_id=message.sender_id, bot_id=message.bot_id,
                             sender_is_verified_non_bot=message.sender_is_verified_non_bot,
                             has_enabled_rule=any(rule.enabled for rule in rules),
                             is_management_command=message.is_management_command,
                             is_text=message.is_text)
        if status == "eligible" and (not isinstance(message.text, str) or not message.text.strip()):
            status = "empty_text"
        if status == "eligible" and len(message.text) > MAX_MESSAGE_CHARS:
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
        payload = {"text": message.text, "sender_id": message.sender_id, "session_origin": message.session_origin}
        if message.original_text is not None:
            payload["original_text"] = message.original_text
        if message.message_time is not None:
            payload["message_time"] = message.message_time
        result = await run_sync_safely(self.queue.enqueue, platform_id=message.platform_id,
            group_id=message.group_id, message_id=message.message_id, capacity=self.capacity,
            payload=payload)
        if result == "queue_full" and self.notify is not None:
            try:
                self.notify("queue_full")
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
        if self.session_gate is None or await self.session_gate(session) is not True:
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
