"""Pure monitoring eligibility decisions; no message handling side effects."""

from dataclasses import dataclass

from .dry_run_queue import SYSTEM_PLATFORM


@dataclass(frozen=True)
class MonitoringPolicy:
    mode: str
    groups: frozenset[tuple[str, str]]


def eligibility(policy: MonitoringPolicy, *, platform_id: str, group_id: str,
                sender_id: str, bot_id: str | None, has_enabled_rule: bool,
                is_management_command: bool, is_text: bool,
                sender_is_verified_non_bot: bool = False) -> str:
    """Return an explicit skip reason or 'eligible'. Inputs are trusted metadata."""
    if policy.mode not in ("allowlist", "blocklist"):
        return "invalid_list_mode"
    if any(not isinstance(x, str) or not x.strip() for x in (platform_id, group_id, sender_id)):
        return "invalid_message_identity"
    if platform_id == SYSTEM_PLATFORM:
        return "invalid_message_identity"
    if sender_is_verified_non_bot is not True and (not isinstance(bot_id, str) or not bot_id.strip()):
        return "bot_identity_unverified"
    if policy.mode == "allowlist" and (platform_id, group_id) not in policy.groups:
        return "group_not_allowed"
    if policy.mode == "blocklist" and (platform_id, group_id) in policy.groups:
        return "group_blocked"
    if isinstance(bot_id, str) and sender_id == bot_id:
        return "self_message"
    if is_management_command is not False:
        return "management_command_or_unknown"
    if is_text is not True:
        return "non_text_or_unknown"
    if has_enabled_rule is not True:
        return "no_enabled_rule"
    return "eligible"
