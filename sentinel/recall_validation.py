"""Side-effect-free recall revalidation against a trusted intent."""

from .action_config import resolve_action_mode, validate_actions
from .mute_validation import MuteCheck


def revalidate_recall(intent, *, rule, default_action_mode=None):
    if not isinstance(intent, dict) or intent.get("action") != "recall":
        return MuteCheck(False, "unsupported_action")
    if any(not isinstance(intent.get(key), str) or not intent[key].strip()
           for key in ("platform_id", "group_id", "message_id", "target_sender_id")):
        return MuteCheck(False, "invalid_message_identity")
    if rule is None or (rule.platform_id, rule.group_id) != (intent["platform_id"], intent["group_id"]):
        return MuteCheck(False, "rule_scope_mismatch")
    if rule.enabled is not True:
        return MuteCheck(False, "rule_disabled")
    if rule.rule_id != intent.get("rule_id") or type(intent.get("rule_version")) is not int or rule.version != intent["rule_version"]:
        return MuteCheck(False, "rule_version_changed")
    try:
        actions = validate_actions(rule.actions)
    except (TypeError, ValueError):
        return MuteCheck(False, "invalid_rule_actions")
    action = next((entry for entry in actions if entry["type"] == "recall"), None)
    try:
        mode = resolve_action_mode(action["mode"], default_action_mode) if action else None
    except ValueError:
        mode = None
    if mode is None or mode != intent.get("mode"):
        return MuteCheck(False, "action_not_allowed")
    return MuteCheck(True, "validated")
