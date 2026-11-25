"""Dry-run LLM decision flow using an injected, isolated model caller."""

import asyncio
import hashlib
import json
import math
import time
from dataclasses import asdict

from .dry_run_worker import Decision
from .decision_errors import AuditSnapshotTooLarge, AuditUnavailable
from .dry_run_queue import MAX_AUDIT_DETAIL_CHARS
from .async_io import run_sync_safely
from .action_config import validate_equal_priority_policy
from .llm_protocol import normalize_assessment_response
from .rule_selection import RuleAssessment, select_rules
from .sentinel_core import RuleSnapshot, validate_plan_proposal
from .rule_snapshot_guard import snapshot_is_current
from .audit_safety import safe_resolved_model
from .prompts import PROMPT_VERSION, select_prompt_language
from .image_fetch import ImageFetchError


_AUDIT_FIELD_KEYS = ("rule_id", "action", "severity")
_AUDIT_VALUE_CHARS = 120
_AUDIT_REASON_CHARS = 500
_SAFE_JSON_INT = 9007199254740991

# An invalid model answer is regenerated a bounded number of times before it is
# rejected. Only failures the model itself can fix are retried; rule or
# administrator state problems are never hidden behind another generation.
MAX_GENERATION_ATTEMPTS = 3
_RETRYABLE_PROTOCOL = frozenset({
    "incomplete_or_failed_response", "incomplete_model_output", "malformed_tool_calls",
    "inconsistent_finish_reason", "missing_or_multiple_assessments",
    "invalid_assessment_tool", "invalid_assessments",
})
_RETRYABLE_VALIDATION = frozenset({
    "invalid_assessment", "invalid_assessment_fields", "invalid_reason", "invalid_verdict",
    "unknown_rule", "invalid_severity", "invalid_duration", "invalid_duration_mode",
    "invalid_parameters",
})


def _audit_value(value):
    """Describe one untrusted model value without storing unbounded data."""
    if value is None or type(value) is bool:
        return {"type": type(value).__name__, "value": value}
    if type(value) is int:
        if -_SAFE_JSON_INT <= value <= _SAFE_JSON_INT:
            return {"type": "int", "value": value}
        return {"type": "int", "digits": len(str(abs(value)))}
    if type(value) is float:
        return {"type": "float", "value": value if math.isfinite(value) else repr(value)}
    if isinstance(value, str):
        return {"type": "str", "value": value[:_AUDIT_VALUE_CHARS], "chars": len(value)}
    return {"type": type(value).__name__}


def _audit_rejected_call(call, *, max_duration_seconds):
    """Keep the fields that explain why an untrusted model verdict was rejected."""
    if not isinstance(call, dict):
        return {"type": type(call).__name__, "max_duration_seconds": max_duration_seconds}
    result = {
        "fields": [str(key)[:64] for key in sorted(call, key=str)[:24]],
        "max_duration_seconds": max_duration_seconds,
    }
    for key in _AUDIT_FIELD_KEYS:
        value = call.get(key)
        if isinstance(value, str):
            result[key] = value[:200]
    if type(call.get("violated")) is bool:
        result["violated"] = call["violated"]
    if "duration_seconds" in call:
        result["duration_seconds"] = _audit_value(call["duration_seconds"])
    reason = call.get("reason")
    if isinstance(reason, str) and reason.strip():
        result["reason"] = reason.strip()[:_AUDIT_REASON_CHARS]
        result["reason_chars"] = len(reason)
    return result


def _input_hash(material):
    """Hash the exact judge inputs without storing message text or context."""
    try:
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bounded_capture(value, limit=2000):
    """Serialize an opt-in debug excerpt without allowing unbounded growth."""
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None
    return text[:limit] + ("...[truncated]" if len(text) > limit else "")


class LlmDecider:
    """Judge one queued message against a frozen set of group rules.

    invoke is responsible for an isolated model call and exposing only the
    proposal tool. Its output is still untrusted and validated here. This
    class does not provide any platform action or ordinary chat tool.
    """

    def __init__(self, rules, invoke, *, model: str, max_duration_seconds: int, queue,
                 equal_priority_policy: str = "execute_all", no_action_reason_enabled: bool = True, image_enabled: bool = False,
                 image_model: str | None = None, image_mode: str = "direct",
                 caption_prompt: str = "", fetcher=None, prefetcher=None, chat_context=None,
                 diagnostic_capture: bool = False,
                 max_generation_attempts: int = MAX_GENERATION_ATTEMPTS,
                 retry_budget_seconds: float | None = None):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be explicitly selected")
        if type(max_duration_seconds) is not int or max_duration_seconds <= 0:
            raise ValueError("invalid duration limit")
        if type(max_generation_attempts) is not int or not 1 <= max_generation_attempts <= 5:
            raise ValueError("invalid generation attempt limit")
        if (retry_budget_seconds is not None
                and (type(retry_budget_seconds) not in (int, float)
                     or not 0 < retry_budget_seconds < float("inf"))):
            raise ValueError("invalid retry budget")
        validate_equal_priority_policy(equal_priority_policy)
        if type(no_action_reason_enabled) is not bool:
            raise ValueError("invalid no-action reason setting")
        if type(image_enabled) is not bool or image_mode not in ("caption", "direct"):
            raise ValueError("invalid image judging setting")
        if type(diagnostic_capture) is not bool:
            raise ValueError("invalid diagnostic capture setting")
        if image_model is not None and (not isinstance(image_model, str) or not image_model.strip()):
            raise ValueError("invalid image model")
        if image_mode == "caption" and image_enabled and not isinstance(caption_prompt, str):
            raise ValueError("invalid image caption prompt")
        self.rules = rules
        self.invoke = invoke
        self.model = model
        self.max_duration_seconds = max_duration_seconds
        self.queue = queue
        self.equal_priority_policy = equal_priority_policy
        self.no_action_reason_enabled = no_action_reason_enabled
        self.image_enabled = image_enabled
        self.image_model = image_model
        self.image_mode = image_mode
        self.caption_prompt = caption_prompt
        self.fetcher = fetcher
        self.prefetcher = prefetcher
        self.chat_context = chat_context
        self.diagnostic_capture = diagnostic_capture
        self.max_generation_attempts = max_generation_attempts
        self.retry_budget_seconds = retry_budget_seconds

    @staticmethod
    def _generation_detail(detail, attempt, failures):
        """Describe earlier rejected generations without growing the audit."""
        if attempt > 1:
            detail["generation_attempts"] = attempt
            detail["generation_reasons"] = list(failures)[:MAX_GENERATION_ATTEMPTS]
        return detail

    def _retry_gate(self, attempt, reason, retryable, started_at, attempt_ms):
        """Return None to regenerate, otherwise why this answer is final."""
        if reason not in retryable:
            return "not_retryable"
        if attempt >= self.max_generation_attempts:
            return "attempts_exhausted"
        if self.retry_budget_seconds is not None:
            remaining = self.retry_budget_seconds - (time.monotonic() - started_at)
            if remaining <= max(1.0, attempt_ms / 1000):
                return "time_budget"
        return None

    def _validate_assessments(self, normalized, *, snapshots, sender):
        """Validate every assessment in one model answer.

        Returns (assessments, proposals, non_hit_reasons) or (None, rejection)
        with rejection a (reason, rejected_call or None) pair.
        """
        assessments, proposals, non_hit_reasons = [], {}, {}
        for assessment in normalized.calls:
            rule_id, outcome = assessment.get("rule_id"), assessment.get("outcome")
            if not isinstance(rule_id, str) or rule_id not in snapshots or outcome not in ("hit", "no_hit", "review"):
                return None, ("invalid_assessment", None)
            if outcome != "hit":
                if set(assessment) not in ({"rule_id", "outcome"}, {"rule_id", "outcome", "reason"}):
                    return None, ("invalid_assessment_fields", None)
                if "reason" in assessment:
                    reason = assessment["reason"]
                    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                        return None, ("invalid_reason", None)
                    if self.no_action_reason_enabled:
                        non_hit_reasons[rule_id] = reason.strip()
                assessments.append(RuleAssessment(rule_id, False if outcome == "no_hit" else None))
                continue
            proposal_call = {**{key: value for key, value in assessment.items() if key != "outcome"},
                             "violated": True}
            validated = validate_plan_proposal(proposal_call, sender_id=sender, rules=snapshots,
                max_duration_seconds=self.max_duration_seconds, decision_backend="llm")
            if not validated.proposal:
                return None, (validated.reason, proposal_call)
            assessments.append(RuleAssessment(rule_id, True))
            proposals[rule_id] = validated.proposal
        return (assessments, proposals, non_hit_reasons), None

    async def __call__(self, item):
        rules = await asyncio.to_thread(self.rules.current_for_group, item.platform_id, item.group_id)
        enabled = [rule for rule in rules if rule.enabled]
        if not enabled:
            return Decision("skipped", "no_enabled_rule", model_attempted=False)
        message = item.payload.get("text")
        message = message if isinstance(message, str) else ""
        raw_images = item.payload.get("image_urls")
        images = (tuple(value for value in raw_images if isinstance(value, str) and value)
                  if isinstance(raw_images, list) else ())
        sender = item.payload.get("sender_id")
        if ((not message.strip() and not images) or not isinstance(sender, str) or not sender.strip()):
            return Decision("failed", "invalid_message_snapshot", model_attempted=False)
        def unavailable_context():
            return Decision("review", json.dumps({
                "review_reason": "chat_context_unavailable",
                "chat_context": {"status": "unavailable",
                                 "count": 0, "chars": 0, "limited": False},
            }), model_attempted=False)

        context = self.chat_context.take(item) if self.chat_context is not None else None
        if self.chat_context is not None and context is None:
            return unavailable_context()
        image_data_urls = None
        image_count = 0
        image_bytes = 0
        image_source = None
        caption = None
        judge_model = self.model
        if images:
            if not self.image_enabled:
                return Decision("skipped", "image_judging_disabled", model_attempted=False)
            if not self.image_model or self.fetcher is None:
                return Decision("skipped", "image_model_unconfigured", model_attempted=False)
            cache_key = (item.platform_id, item.group_id, item.message_id)
            cached = await self.prefetcher.consume(cache_key) if self.prefetcher is not None else None
            if cached is not None:
                image_source = "prefetch"
                if cached.code:
                    return Decision("skipped", cached.code, model_attempted=False)
                fetched = cached.images
            else:
                image_source = "inline"
                try:
                    fetched = await self.fetcher.fetch(list(images))
                except ImageFetchError as error:
                    return Decision("skipped", error.code, model_attempted=False)
                except Exception:
                    return Decision("skipped", "image_fetch_failed", model_attempted=False)
            if not fetched:
                return Decision("skipped", "image_fetch_failed", model_attempted=False)
            previews = [image.preview for image in fetched if getattr(image, "preview", None)]
            if previews:
                try:
                    await run_sync_safely(self.queue.record_previews, platform_id=item.platform_id,
                                          group_id=item.group_id, message_id=item.message_id,
                                          previews=previews)
                except Exception:
                    # Previews are a convenience only; never block judging.
                    pass
            image_data_urls = [image.data_url for image in fetched]
            image_count = len(image_data_urls)
            image_bytes = sum(image.byte_count for image in fetched)
            if self.image_mode == "caption":
                try:
                    caption_response = await self.invoke.caption(model=self.image_model,
                        prompt=self.caption_prompt, image_urls=image_data_urls)
                except Exception:
                    return Decision("skipped", "image_caption_failed", model_attempted=False)
                caption = getattr(caption_response, "completion_text", None)
                if not isinstance(caption, str) or not caption.strip():
                    return Decision("skipped", "image_caption_empty", model_attempted=False)
                caption = caption.strip()[:4000]
            else:
                judge_model = self.image_model
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
            if mute and mute["duration_mode"] != "cumulative":
                data["duration_mode"] = mute["duration_mode"]
                if mute["duration_mode"] == "strict":
                    data["severity_levels"] = list(mute["severity_seconds"])
                else:
                    data["duration_limit_seconds"] = self.max_duration_seconds
            model_rules.append(data)
        language = select_prompt_language([rule.text for rule in enabled], message or caption or "")
        context_prompt = context.prompt_data() if context is not None else None
        input_material = {"message": message, "rules": model_rules,
                          "language": language, "prompt_version": PROMPT_VERSION}
        if caption:
            input_material["caption"] = caption
        if context_prompt is not None:
            input_material["chat_context"] = context_prompt
        if images:
            input_material["image_count"] = image_count
            input_material["image_bytes"] = image_bytes
        input_hash = _input_hash(input_material)
        start_detail = {"backend": "llm", "model": judge_model, "rules": rule_data,
                        "sender_id": sender, "prompt_language": language, "prompt_version": PROMPT_VERSION}
        if input_hash is not None:
            start_detail["input_hash"] = input_hash
        if context is not None:
            start_detail["chat_context"] = context.audit()
        if images:
            start_detail["image"] = {"mode": self.image_mode, "model": self.image_model, "count": image_count,
                                     "bytes": image_bytes, "source": image_source,
                                     "caption_chars": len(caption) if caption else 0}
        start_detail = json.dumps(start_detail, ensure_ascii=False, allow_nan=False)
        if len(start_detail) > MAX_AUDIT_DETAIL_CHARS:
            raise AuditSnapshotTooLarge("rule snapshot exceeds audit limit")
        try:
            await run_sync_safely(self.queue.record_evaluation_start, item, start_detail)
        except Exception:
            raise AuditUnavailable("evaluation audit unavailable") from None
        request_started = time.monotonic()
        attempt = 0
        failures = []
        total_latency_ms = 0.0
        invoke_kwargs = {"model": judge_model, "message": message, "rules": model_rules,
                         "language": language}
        if context_prompt is not None:
            invoke_kwargs["chat_context"] = context_prompt
        if images and self.image_mode == "direct":
            invoke_kwargs["image_urls"] = image_data_urls
        elif images and caption:
            invoke_kwargs["image_caption"] = caption
        while True:
            attempt += 1
            invoke_kwargs["retry_reason"] = failures[-1] if failures else None
            started = time.monotonic()
            response = await self.invoke(**invoke_kwargs)
            attempt_ms = round((time.monotonic() - started) * 1000, 3)
            total_latency_ms += attempt_ms
            normalized = normalize_assessment_response(response)
            detail = {"backend": "llm", "model": judge_model, "rules": rule_data,
                      "prompt_language": language, "prompt_version": PROMPT_VERSION,
                      "response_status": normalized.reason, "model_latency_ms": round(total_latency_ms, 3),
                      "message_chars": len(message)}
            if input_hash is not None:
                detail["input_hash"] = input_hash
            if normalized.diagnostics and normalized.status in ("failed", "rejected"):
                detail["protocol"] = normalized.diagnostics
            if self.diagnostic_capture and normalized.status in ("failed", "rejected"):
                raw_arguments = _bounded_capture(getattr(response, "tools_call_args", None))
                if raw_arguments is not None:
                    detail["raw_tool_arguments"] = raw_arguments
                completion = getattr(response, "completion_text", None)
                if isinstance(completion, str) and completion.strip():
                    detail["raw_completion_text"] = completion.strip()[:1000]
            if context is not None:
                detail["chat_context"] = context.audit()
            if normalized.status == "review":
                detail["review_reason"] = normalized.reason
            if images:
                detail["image"] = {"mode": self.image_mode, "model": self.image_model, "count": image_count,
                                   "bytes": image_bytes, "source": image_source,
                                   "caption_chars": len(caption) if caption else 0}
            raw = getattr(response, "raw_completion", None)
            model_name = raw.get("model") if isinstance(raw, dict) else getattr(raw, "model", None)
            detail["provider_id"] = judge_model
            detail["resolved_model"] = safe_resolved_model(model_name, message)
            usage = getattr(response, "usage", None)
            if usage is not None:
                tokens = {key: getattr(usage, key) for key in ("input_other", "input_cached", "output")
                          if type(getattr(usage, key, None)) is int and getattr(usage, key) >= 0}
                if tokens:
                    detail["usage"] = tokens
            status = normalized.status
            if status != "assessments":
                gate = (self._retry_gate(attempt, normalized.reason, _RETRYABLE_PROTOCOL,
                                         request_started, attempt_ms)
                        if status in ("failed", "rejected") else "not_retryable")
                if gate is None:
                    failures.append(normalized.reason)
                    continue
                if gate == "time_budget":
                    detail["retry_skipped"] = gate
                detail = self._generation_detail(detail, attempt, failures + [normalized.reason])
                return Decision(status, json.dumps(detail, ensure_ascii=False))
            validated, rejection = self._validate_assessments(normalized, snapshots=snapshots, sender=sender)
            if rejection is not None:
                reason, rejected_call = rejection
                detail["validation"] = reason
                if rejected_call is not None:
                    detail["rejected_call"] = _audit_rejected_call(
                        rejected_call, max_duration_seconds=self.max_duration_seconds)
                gate = self._retry_gate(attempt, reason, _RETRYABLE_VALIDATION, request_started, attempt_ms)
                if gate is None:
                    failures.append(reason)
                    continue
                if gate == "time_budget":
                    detail["retry_skipped"] = gate
                detail = self._generation_detail(detail, attempt, failures + [reason])
                return Decision("rejected", json.dumps(detail, ensure_ascii=False))
            assessments, proposals, non_hit_reasons = validated
            selection = select_rules(enabled_priorities={rule.rule_id: rule.priority for rule in enabled},
                                     assessments=assessments, equal_priority_policy=self.equal_priority_policy)
            detail["selection_reason"] = selection.reason
            detail["assessments"] = [{"rule_id": item.rule_id, "violated": item.violated} for item in assessments]
            detail["matched_rules"] = [item.rule_id for item in assessments if item.violated is True]
            detail["equal_priority_policy"] = self.equal_priority_policy
            status = "proposed" if selection.status == "selected" else selection.status
            if status == "no_action" and non_hit_reasons:
                detail["reason"] = "; ".join(f"{rule_id}: {reason}" for rule_id, reason in non_hit_reasons.items())
            if selection.rule_ids:
                selected = [asdict(proposals[rule_id]) for rule_id in selection.rule_ids]
                detail["selected_rules"] = list(selection.rule_ids)
                detail["proposals"] = selected
                detail["proposal"] = selected[0]
                detail["peer_tie"] = selection.peer_tie
            elif status == "review":
                detail["review_reason"] = selection.reason
            if status == "no_action":
                detail["no_action_reason_enabled"] = self.no_action_reason_enabled
            if status in ("proposed", "no_action"):
                current = await snapshot_is_current(self.rules, item, enabled)
                detail["snapshot_current"] = current
                if not current:
                    detail["review_reason"] = "rule_snapshot_changed"
                    status = "review"
            versions = tuple((rule.rule_id, rule.version) for rule in enabled)
            detail = self._generation_detail(detail, attempt, failures)
            return Decision(status, json.dumps(detail, ensure_ascii=False, allow_nan=False),
                            rule_versions=versions)
