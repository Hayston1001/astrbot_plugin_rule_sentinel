"""Conservative selection across independently evaluated rule matches.

This module does not call a model. The caller must supply an assessment for
every enabled rule from the same message snapshot. Unknown or conflicting
assessments require review, never automatic no-action or punishment.
"""

from dataclasses import dataclass
from typing import Mapping

from .action_config import validate_equal_priority_policy


@dataclass(frozen=True)
class RuleAssessment:
    rule_id: str
    violated: bool | None


@dataclass(frozen=True)
class Selection:
    status: str
    rule_ids: tuple[str, ...]
    reason: str
    peer_tie: bool = False


def select_rules(*, enabled_priorities: Mapping[str, int], assessments: list[RuleAssessment],
                 equal_priority_policy: str = "execute_all") -> Selection:
    """Pick the highest-priority matching rules.

    A tie at the top returns every tied rule. Under the "confirm" policy the
    selection is flagged so every tied rule waits for a manual confirmation.
    """
    validate_equal_priority_policy(equal_priority_policy)
    if not enabled_priorities or any(
        not isinstance(rule_id, str) or not rule_id.strip() or type(priority) is not int
        for rule_id, priority in enabled_priorities.items()
    ):
        raise ValueError("invalid enabled rule priorities")
    if not isinstance(assessments, list) or any(not isinstance(item, RuleAssessment) for item in assessments):
        raise ValueError("invalid assessments")
    ids = [item.rule_id for item in assessments]
    if any(not isinstance(rule_id, str) or not rule_id.strip() for rule_id in ids):
        return Selection("review", (), "invalid_rule_identity")
    if len(ids) != len(set(ids)) or set(ids) != set(enabled_priorities):
        return Selection("review", (), "incomplete_or_duplicate_assessment")
    if any(item.violated is not True and item.violated is not False for item in assessments):
        return Selection("review", (), "uncertain_assessment")
    matched = [item.rule_id for item in assessments if item.violated is True]
    if not matched:
        return Selection("no_action", (), "no_matching_rules")
    highest = max(enabled_priorities[rule_id] for rule_id in matched)
    winners = tuple(rule_id for rule_id in matched if enabled_priorities[rule_id] == highest)
    if len(winners) == 1:
        return Selection("selected", winners, "highest_priority_match")
    return Selection("selected", winners, "equal_priority_match", peer_tie=equal_priority_policy == "confirm")
