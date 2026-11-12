"""Offline-wired Jev dry-run decision flow with an injected async transport.

This adapter supports model selection and administrator priority across
strict-mode rules. Autonomous durations require a separate adapter. It does not
send messages or invoke moderation actions.
"""

import json
import time
from dataclasses import asdict

from .dry_run_worker import Decision
from .decision_errors import AuditSnapshotTooLarge, AuditUnavailable
from .dry_run_queue import MAX_AUDIT_DETAIL_CHARS
from .async_io import run_sync_safely
from .jev_protocol import InvalidJevAction, JevBatchChoices, build_group_request, build_priority_request, parse_batch_response, parse_response, parse_priority_response
from .sentinel_core import RuleSnapshot, validate_plan_proposal
from .rule_snapshot_guard import snapshot_is_current
from .audit_safety import safe_resolved_model


class JevDecider:
    def __init__(self, rules, send_request, *, model: str, max_duration_seconds: int, queue, strategy: str = "model"):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be explicitly selected")
        if type(max_duration_seconds) is not int or max_duration_seconds <= 0:
            raise ValueError("invalid duration limit")
        if strategy not in ("model", "priority"):
            raise ValueError("invalid rule selection strategy")
        self.rules = rules
        self.send_request = send_request
        self.model = model
        self.max_duration_seconds = max_duration_seconds
        self.queue = queue
        self.strategy = strategy

    async def __call__(self, item):
        # Reading SQLite runs off the event loop so normal chat is not blocked.
        import asyncio
        rules = await asyncio.to_thread(self.rules.current_for_group, item.platform_id, item.group_id)
        enabled = [rule for rule in rules if rule.enabled]
        if not enabled:
            return Decision("skipped", "no_enabled_rule", model_attempted=False)
        if any(action["type"] == "mute" and action["duration_mode"] != "strict"
               for rule in enabled for action in rule.actions):
            return Decision("review", "unsupported_jev_rule_policy", model_attempted=False)
        payload = item.payload
        message = payload.get("text")
        sender = payload.get("sender_id")
        if not isinstance(message, str) or not message.strip() or not isinstance(sender, str) or not sender.strip():
            return Decision("failed", "invalid_message_snapshot", model_attempted=False)
        rules_data = [asdict(rule) for rule in enabled]
        choices = (build_priority_request if self.strategy == "priority" else build_group_request)(
            model=self.model, message=message, rules=rules_data)
        def candidate_table(choice, fallback_rule=None):
            result = {}
            for option in (choice.action_by_option or {}):
                rule_id = choice.rule_by_option[option] if choice.rule_by_option is not None else fallback_rule
                rule = next(rule for rule in enabled if rule.rule_id == rule_id)
                value = {"rule_id": rule_id, "rule_version": rule.version}
                if option in choice.severity_by_option:
                    action = next(item for item in rule.actions if item["type"] == "mute")
                    value["severity"] = choice.severity_by_option[option]
                    value["duration_seconds"] = action["severity_seconds"][value["severity"]]
                result[option] = value
            return result
        candidate_tables = ({question: candidate_table(choice, rule_id)
                             for question, (rule_id, choice) in choices.by_question.items()}
                            if self.strategy == "priority" else
                            {question: candidate_table(choice) for question, choice in choices.by_question.items()}
                            if isinstance(choices, JevBatchChoices) else {"decision": candidate_table(choices)})
        start_detail = json.dumps({"backend": "jev", "model": self.model, "strategy": self.strategy, "rules": rules_data,
                                   "sender_id": sender, "candidate_options": candidate_tables}, ensure_ascii=False, allow_nan=False)
        if len(start_detail) > MAX_AUDIT_DETAIL_CHARS:
            raise AuditSnapshotTooLarge("rule snapshot exceeds audit limit")
        try:
            await run_sync_safely(self.queue.record_evaluation_start, item, start_detail)
        except Exception:
            raise AuditUnavailable("evaluation audit unavailable") from None
        started = time.monotonic()
        response = await self.send_request(choices.request)
        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        try:
            result = (parse_priority_response(response, choices, {rule.rule_id: rule.priority for rule in enabled})
                      if self.strategy == "priority" else parse_batch_response(response, choices)
                      if isinstance(choices, JevBatchChoices) else parse_response(response, choices, ""))
        except InvalidJevAction:
            return Decision("rejected", json.dumps({"backend": "jev", "model": self.model,
                "rules": rules_data, "validation": "unknown_choice"}, ensure_ascii=False))
        if self.strategy == "priority":
            option_mapping = {rule_id: dict(rule_choices.severity_by_option)
                              for rule_id, rule_choices in choices.by_question.values()}
            evidence = {"selection_reason": result["selection_reason"], "assessments": result["assessments"],
                        "severity_options_by_rule": option_mapping}
        else:
            evidence = {"choice": result["choice"], "confidence": result["confidence"],
                        "probabilities": result["probabilities"],
                        "candidate_options": candidate_tables if isinstance(choices, JevBatchChoices) else candidate_tables["decision"]}
            if "selected_choices" in result:
                evidence["selected_choices"] = result["selected_choices"]
        detail = {"backend": "jev", "model": safe_resolved_model(result["model"], message),
                  "requested_model": self.model, "rules": rules_data,
                  "strategy": self.strategy, **evidence, "reason": None, "model_latency_ms": elapsed_ms}
        usage = response.get("usage") if isinstance(response, dict) else None
        if isinstance(usage, dict):
            tokens = {key: usage[key] for key in ("input_tokens", "output_tokens")
                      if type(usage.get(key)) is int and usage[key] >= 0}
            if tokens:
                detail["usage"] = tokens
        if result["status"] == "proposed":
            snapshots = {rule.rule_id: RuleSnapshot(rule.rule_id, rule.version, rule.enabled,
                                    frozenset(rule.allowed_actions), rule.duration_mode,
                                     rule.severity_seconds, rule.actions) for rule in enabled}
            validated = validate_plan_proposal(result["call"], sender_id=sender,
                rules=snapshots, max_duration_seconds=self.max_duration_seconds,
                decision_backend="jev")
            detail["validation"] = validated.reason
            if validated.proposal:
                detail["proposal"] = asdict(validated.proposal)
            status = validated.status
        else:
            status = result["status"]
        if status in ("proposed", "no_action"):
            current = await snapshot_is_current(self.rules, item, enabled)
            detail["snapshot_current"] = current
            if not current:
                detail["review_reason"] = "rule_snapshot_changed"
                status = "review"
        versions = tuple((rule.rule_id, rule.version) for rule in enabled)
        return Decision(status, json.dumps(detail, ensure_ascii=False, allow_nan=False), rule_versions=versions)
