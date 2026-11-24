"""Side-effect-free validation of dry-run moderation proposals.

This module does not authenticate administrators, read live rule state, or
perform platform actions. Callers must obtain a trusted rule snapshot and
perform those checks separately before any real action.
"""

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class RuleSnapshot:
    rule_id: str
    version: int
    enabled: bool
    allowed_actions: frozenset[str]
    duration_mode: str
    severity_seconds: Mapping[str, int]
    actions: tuple[dict, ...] = ()


@dataclass(frozen=True)
class ValidationResult:
    status: str
    reason: str
    proposal: "PlanProposal | None" = None


@dataclass(frozen=True)
class PlanProposal:
    rule_id: str
    rule_version: int
    target_sender_id: str
    duration_seconds: int | None
    reason: str | None
    decision_backend: str


def validate_plan_proposal(call, *, sender_id, rules, max_duration_seconds, decision_backend="llm"):
    """Validate a model verdict without accepting action or target selection."""
    def reject(reason):
        return ValidationResult("rejected", reason)

    if decision_backend != "llm":
        return reject("invalid_decision_backend")
    if not isinstance(sender_id, str) or not sender_id.strip():
        return reject("invalid_sender")
    if type(max_duration_seconds) is not int or max_duration_seconds <= 0:
        return reject("invalid_duration_limit")
    if not isinstance(call, dict):
        return reject("invalid_parameters")
    rule_id = call.get("rule_id")
    rule = rules.get(rule_id) if isinstance(rule_id, str) else None
    if rule is None or rule.rule_id != rule_id:
        return reject("unknown_rule")
    if rule.enabled is not True or type(rule.version) is not int or rule.version <= 0:
        return reject("invalid_rule")
    reason = call.get("reason")
    if call.get("violated") is not True:
        return reject("invalid_verdict")
    fields = {"rule_id", "violated"}
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        return reject("invalid_reason")
    reason = reason.strip()
    fields.add("reason")
    mute = next((item for item in rule.actions if item.get("type") == "mute"), None)
    duration = None
    if mute is not None:
        if mute.get("duration_mode") == "strict":
            fields.add("severity")
            severity = call.get("severity")
            mapping = mute.get("severity_seconds", {})
            if not isinstance(severity, str) or not isinstance(mapping, Mapping) or severity not in mapping:
                return reject("invalid_severity")
            duration = mapping[severity]
        elif mute.get("duration_mode") == "autonomous":
            fields.add("duration_seconds")
            duration = call.get("duration_seconds")
        elif mute.get("duration_mode") != "cumulative":
            return reject("invalid_duration_mode")
        if mute.get("duration_mode") != "cumulative" and (type(duration) is not int or not 0 < duration <= max_duration_seconds):
            return reject("invalid_duration")
    if set(call) != fields:
        return reject("invalid_parameters")
    return ValidationResult("proposed", "validated_plan_proposal",
                            PlanProposal(rule_id, rule.version, sender_id, duration, reason, decision_backend))
