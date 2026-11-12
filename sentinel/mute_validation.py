"""Side-effect-free mute revalidation."""

from dataclasses import dataclass

from .action_config import resolve_action_mode, validate_actions


@dataclass(frozen=True)
class MuteCheck:
    allowed: bool
    reason: str


def revalidate_mute(intent, *, rule, max_duration_seconds, default_action_mode=None):
    """Recheck a server-persisted intent against the current rule.

    This helper does not verify platform permissions or persist an audit intent.
    Both are mandatory before any real action and must be done by the caller.
    """
    def reject(reason):
        return MuteCheck(False, reason)

    if not isinstance(intent, dict) or intent.get("action") != "mute":
        return reject("unsupported_action")
    platform_id, group_id, sender_id = (intent.get(key) for key in ("platform_id", "group_id", "target_sender_id"))
    if any(not isinstance(value, str) or not value.strip() for value in (platform_id, group_id, sender_id)):
        return reject("invalid_message_identity")
    if rule is None or rule.platform_id != platform_id or rule.group_id != group_id:
        return reject("rule_scope_mismatch")
    if rule.enabled is not True:
        return reject("rule_disabled")
    if (not isinstance(intent.get("rule_id"), str) or intent["rule_id"] != rule.rule_id
            or type(intent.get("rule_version")) is not int or intent["rule_version"] != rule.version):
        return reject("rule_version_changed")
    try:
        actions = validate_actions(rule.actions)
    except (TypeError, ValueError):
        return reject("invalid_rule_actions")
    action = next((item for item in actions if item["type"] == "mute"), None)
    try:
        mode = resolve_action_mode(action["mode"], default_action_mode) if action else None
    except ValueError:
        mode = None
    if mode is None or intent.get("mode") != mode:
        return reject("action_not_allowed")
    duration = intent.get("duration_seconds")
    if (type(max_duration_seconds) is not int or not 0 < max_duration_seconds <= 2592000
            or type(duration) is not int or not 0 < duration <= max_duration_seconds):
        return reject("invalid_duration")
    if action["duration_mode"] == "strict" and duration not in action["severity_seconds"].values():
        return reject("duration_not_in_rule")
    return MuteCheck(True, "validated")
