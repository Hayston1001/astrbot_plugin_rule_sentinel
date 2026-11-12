"""Conservative selection across independently evaluated rule matches.

This module does not call a model. The caller must supply an assessment for
every enabled rule from the same message snapshot. Unknown or conflicting
assessments require review, never automatic no-action or punishment.
"""

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class RuleAssessment:
    rule_id: str
    violated: bool | None


@dataclass(frozen=True)
class Selection:
    status: str
    rule_id: str | None
    reason: str


def select_rule(*, enabled_priorities: Mapping[str, int], assessments: list[RuleAssessment],
                strategy: str, model_choice: str | None = None) -> Selection:
    if strategy not in ("priority", "model"):
        raise ValueError("invalid selection strategy")
    if not enabled_priorities or any(
        not isinstance(rule_id, str) or not rule_id.strip() or type(priority) is not int
        for rule_id, priority in enabled_priorities.items()
    ):
        raise ValueError("invalid enabled rule priorities")
    if not isinstance(assessments, list) or any(not isinstance(item, RuleAssessment) for item in assessments):
        raise ValueError("invalid assessments")
    ids = [item.rule_id for item in assessments]
    if any(not isinstance(rule_id, str) or not rule_id.strip() for rule_id in ids):
        return Selection("review", None, "invalid_rule_identity")
    if len(ids) != len(set(ids)) or set(ids) != set(enabled_priorities):
        return Selection("review", None, "incomplete_or_duplicate_assessment")
    if any(item.violated is not True and item.violated is not False for item in assessments):
        return Selection("review", None, "uncertain_assessment")
    matched = [item.rule_id for item in assessments if item.violated is True]
    if not matched:
        if model_choice is not None:
            return Selection("review", None, "choice_without_match")
        return Selection("no_action", None, "no_matching_rules")
    if strategy == "model":
        if model_choice not in matched:
            return Selection("review", None, "invalid_model_choice")
        return Selection("selected", model_choice, "model_selected_matching_rule")
    highest = max(enabled_priorities[rule_id] for rule_id in matched)
    winners = [rule_id for rule_id in matched if enabled_priorities[rule_id] == highest]
    if len(winners) != 1:
        return Selection("review", None, "equal_priority_conflict")
    return Selection("selected", winners[0], "highest_priority_match")
