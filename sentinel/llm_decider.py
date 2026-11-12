"""Dry-run LLM decision flow using an injected, isolated model caller."""

import asyncio
import json
import time
from dataclasses import asdict

from .dry_run_worker import Decision
from .decision_errors import AuditSnapshotTooLarge, AuditUnavailable
from .dry_run_queue import MAX_AUDIT_DETAIL_CHARS
from .async_io import run_sync_safely
from .llm_protocol import normalize_priority_response, normalize_response
from .rule_selection import RuleAssessment, select_rule
from .sentinel_core import RuleSnapshot, validate_plan_proposal
from .rule_snapshot_guard import snapshot_is_current
from .audit_safety import safe_resolved_model
from .prompts import select_prompt_language


class LlmDecider:
    """Judge one queued message against a frozen set of group rules.

    invoke is responsible for an isolated model call and exposing only the
    proposal tool. Its output is still untrusted and validated here. This
    class does not provide any platform action or ordinary chat tool.
    """

    def __init__(self, rules, invoke, *, model: str, max_duration_seconds: int, queue, strategy: str = "model",
                 no_action_reason_enabled: bool = True):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be explicitly selected")
        if type(max_duration_seconds) is not int or max_duration_seconds <= 0:
            raise ValueError("invalid duration limit")
        if strategy not in ("model", "priority"):
            raise ValueError("invalid rule selection strategy")
        if type(no_action_reason_enabled) is not bool:
            raise ValueError("invalid no-action reason setting")
        self.rules = rules
        self.invoke = invoke
        self.model = model
        self.max_duration_seconds = max_duration_seconds
        self.queue = queue
        self.strategy = strategy
        self.no_action_reason_enabled = no_action_reason_enabled

    async def __call__(self, item):
        rules = await asyncio.to_thread(self.rules.current_for_group, item.platform_id, item.group_id)
        enabled = [rule for rule in rules if rule.enabled]
        if not enabled:
            return Decision("skipped", "no_enabled_rule", model_attempted=False)
        message = item.payload.get("text")
        sender = item.payload.get("sender_id")
        if not isinstance(message, str) or not message.strip() or not isinstance(sender, str) or not sender.strip():
            return Decision("failed", "invalid_message_snapshot", model_attempted=False)
        # Copy data before the asynchronous model call so later edits do not
        # change which version was actually judged.
        snapshots = {rule.rule_id: RuleSnapshot(rule.rule_id, rule.version, rule.enabled,
                     frozenset(rule.allowed_actions), rule.duration_mode, dict(rule.severity_seconds), rule.actions)
                     for rule in enabled}
        rule_data = [asdict(rule) for rule in enabled]
        model_rules = []
        for rule in enabled:
            data = {"rule_id": rule.rule_id, "text": rule.text}
            mute = next((action for action in rule.actions if action["type"] == "mute"), None)
            if mute:
                data["duration_mode"] = mute["duration_mode"]
                if mute["duration_mode"] == "strict":
                    data["severity_levels"] = list(mute["severity_seconds"])
            model_rules.append(data)
        language = select_prompt_language([rule.text for rule in enabled], message)
        start_detail = json.dumps({"backend": "llm", "model": self.model, "strategy": self.strategy, "rules": rule_data,
                                   "sender_id": sender, "prompt_language": language}, ensure_ascii=False, allow_nan=False)
        if len(start_detail) > MAX_AUDIT_DETAIL_CHARS:
            raise AuditSnapshotTooLarge("rule snapshot exceeds audit limit")
        try:
            await run_sync_safely(self.queue.record_evaluation_start, item, start_detail)
        except Exception:
            raise AuditUnavailable("evaluation audit unavailable") from None
        started = time.monotonic()
        response = await self.invoke(model=self.model, message=message,
                                     rules=model_rules, strategy=self.strategy, language=language)
        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        normalized = normalize_priority_response(response) if self.strategy == "priority" else normalize_response(response)
        detail = {"backend": "llm", "model": self.model, "rules": rule_data,
                  "strategy": self.strategy, "prompt_language": language, "response_status": normalized.reason,
                  "model_latency_ms": elapsed_ms, "message_chars": len(message)}
        raw = getattr(response, "raw_completion", None)
        model_name = raw.get("model") if isinstance(raw, dict) else getattr(raw, "model", None)
        detail["provider_id"] = self.model
        detail["resolved_model"] = safe_resolved_model(model_name, message)
        usage = getattr(response, "usage", None)
        if usage is not None:
            tokens = {key: getattr(usage, key) for key in ("input_other", "input_cached", "output")
                      if type(getattr(usage, key, None)) is int and getattr(usage, key) >= 0}
            if tokens:
                detail["usage"] = tokens
        status = normalized.status
        if status == "no_action" and self.no_action_reason_enabled and normalized.explanation:
            detail["reason"] = normalized.explanation
        if status == "assessments":
            assessments, proposals, non_hit_reasons = [], {}, {}
            for assessment in normalized.calls:
                rule_id, outcome = assessment.get("rule_id"), assessment.get("outcome")
                if not isinstance(rule_id, str) or rule_id not in snapshots or outcome not in ("hit", "no_hit", "review"):
                    return Decision("rejected", json.dumps({**detail, "validation": "invalid_assessment"}, ensure_ascii=False))
                if outcome != "hit":
                    if set(assessment) not in ({"rule_id", "outcome"}, {"rule_id", "outcome", "reason"}):
                        return Decision("rejected", json.dumps({**detail, "validation": "invalid_assessment_fields"}, ensure_ascii=False))
                    if "reason" in assessment:
                        reason = assessment["reason"]
                        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                            return Decision("rejected", json.dumps({**detail, "validation": "invalid_reason"}, ensure_ascii=False))
                        if self.no_action_reason_enabled:
                            non_hit_reasons[rule_id] = reason.strip()
                    assessments.append(RuleAssessment(rule_id, False if outcome == "no_hit" else None))
                    continue
                proposal_call = {**{key: value for key, value in assessment.items() if key != "outcome"}, "violated": True}
                validated = validate_plan_proposal(proposal_call, sender_id=sender, rules=snapshots,
                    max_duration_seconds=self.max_duration_seconds, decision_backend="llm")
                if not validated.proposal:
                    return Decision("rejected", json.dumps({**detail, "validation": validated.reason}, ensure_ascii=False))
                assessments.append(RuleAssessment(rule_id, True))
                proposals[rule_id] = validated.proposal
            selection = select_rule(enabled_priorities={rule.rule_id: rule.priority for rule in enabled},
                                    assessments=assessments, strategy="priority")
            detail["selection_reason"] = selection.reason
            detail["assessments"] = [{"rule_id": item.rule_id, "violated": item.violated} for item in assessments]
            status = "proposed" if selection.status == "selected" else selection.status
            if status == "no_action" and non_hit_reasons:
                detail["reason"] = "; ".join(f"{rule_id}: {reason}" for rule_id, reason in non_hit_reasons.items())
            if selection.rule_id is not None:
                proposal = asdict(proposals[selection.rule_id])
                detail["proposal"] = proposal
        if status == "proposed":
            if normalized.status != "assessments":
                validated = validate_plan_proposal(normalized.calls[0], sender_id=sender, rules=snapshots,
                    max_duration_seconds=self.max_duration_seconds, decision_backend="llm")
                detail["validation"] = validated.reason
                status = validated.status
                if validated.proposal:
                    detail["proposal"] = asdict(validated.proposal)
        if status == "no_action":
            detail["no_action_reason_enabled"] = self.no_action_reason_enabled
        if status in ("proposed", "no_action"):
            current = await snapshot_is_current(self.rules, item, enabled)
            detail["snapshot_current"] = current
            if not current:
                detail["review_reason"] = "rule_snapshot_changed"
                status = "review"
        versions = tuple((rule.rule_id, rule.version) for rule in enabled)
        return Decision(status, json.dumps(detail, ensure_ascii=False, allow_nan=False), rule_versions=versions)
