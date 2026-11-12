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
class Proposal:
    rule_id: str
    rule_version: int
    action: str
    target_sender_id: str
    duration_seconds: int | None
    reason: str | None
    decision_backend: str
    mode: str = "auto"
    template: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    status: str
    reason: str
    proposal: Proposal | None = None


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
        else:
            return reject("invalid_duration_mode")
        if type(duration) is not int or not 0 < duration <= max_duration_seconds:
            return reject("invalid_duration")
    if set(call) != fields:
        return reject("invalid_parameters")
    return ValidationResult("proposed", "validated_plan_proposal",
                            PlanProposal(rule_id, rule.version, sender_id, duration, reason, decision_backend))


def validate_proposals(
    calls: object,
    *,
    sender_id: str,
    rules: Mapping[str, RuleSnapshot],
    max_duration_seconds: int,
    decision_backend: str = "llm",
) -> ValidationResult:
    """Validate one model response against a trusted, per-message snapshot.

    Calls are untrusted dictionaries. The caller supplies the sender and rules;
    the model cannot select a target or provide a rule version. A no-action
    result must not be used for model errors or incomplete tool call streams.
    decision_backend comes from trusted configuration, never model output.
    """
    def reject(reason: str) -> ValidationResult:
        return ValidationResult("rejected", reason)

    if decision_backend != "llm":
        return reject("invalid_decision_backend")
    if not isinstance(calls, list):
        return reject("invalid_calls")
    if not calls:
        return ValidationResult("no_action", "no_tool_calls")
    if len(calls) != 1:
        return reject("multiple_tool_calls")
    if not isinstance(sender_id, str) or not sender_id.strip():
        return reject("invalid_sender")
    if type(max_duration_seconds) is not int or max_duration_seconds <= 0:
        return reject("invalid_duration_limit")

    call = calls[0]
    if not isinstance(call, dict) or call.get("action") not in ("mute", "reply", "recall"):
        return reject("unsupported_action")
    rule_id = call.get("rule_id")
    if not isinstance(rule_id, str):
        return reject("invalid_rule")
    rule = rules.get(rule_id)
    if rule is None or rule.rule_id != rule_id:
        return reject("unknown_rule")
    action = call["action"]
    if rule.enabled is not True or action not in rule.allowed_actions:
        return reject("rule_not_enabled_for_action")
    if type(rule.version) is not int or rule.version <= 0:
        return reject("invalid_rule_version")
    reason = call.get("reason")
    common = {"action", "rule_id"}
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        return reject("invalid_reason")
    reason = reason.strip()
    common.add("reason")
    selected = next((item for item in rule.actions if item.get("type") == action), None)
    if rule.actions and selected is None:
        return reject("rule_not_enabled_for_action")
    if selected is None and action != "mute":
        return reject("rule_not_enabled_for_action")
    mode = selected["mode"] if selected else "auto"
    if action in ("reply", "recall"):
        if set(call) != common:
            return reject("invalid_parameters")
        return ValidationResult("proposed", "validated_dry_run_proposal",
            Proposal(rule_id, rule.version, action, sender_id, None, reason, decision_backend,
                     mode, selected.get("template") if action == "reply" else None))
    duration_mode = selected["duration_mode"] if selected else rule.duration_mode
    severity_seconds = selected["severity_seconds"] if selected else rule.severity_seconds
    if duration_mode == "strict":
        if set(call) != common | {"severity"}:
            return reject("invalid_parameters")
        severity = call.get("severity")
        if not isinstance(severity, str) or severity not in severity_seconds:
            return reject("invalid_severity")
        duration = severity_seconds[severity]
    elif duration_mode == "autonomous":
        if set(call) != common | {"duration_seconds"}:
            return reject("invalid_parameters")
        duration = call.get("duration_seconds")
    else:
        return reject("invalid_duration_mode")

    if type(duration) is not int or not 0 < duration <= max_duration_seconds:
        return reject("invalid_duration")
    return ValidationResult(
        "proposed",
        "validated_dry_run_proposal",
        Proposal(rule_id, rule.version, "mute", sender_id, duration, reason, decision_backend, mode),
    )
