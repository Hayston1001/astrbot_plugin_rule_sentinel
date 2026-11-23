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
from .llm_protocol import normalize_priority_response, normalize_response
from .rule_selection import RuleAssessment, select_rule
from .sentinel_core import RuleSnapshot, validate_plan_proposal
from .rule_snapshot_guard import snapshot_is_current
from .audit_safety import safe_resolved_model
from .prompts import PROMPT_VERSION, select_prompt_language
from .image_fetch import ImageFetchError


_AUDIT_FIELD_KEYS = ("rule_id", "action", "severity")
_AUDIT_VALUE_CHARS = 120
_AUDIT_REASON_CHARS = 500
_SAFE_JSON_INT = 9007199254740991


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

    def __init__(self, rules, invoke, *, model: str, max_duration_seconds: int, queue, strategy: str = "model",
                 no_action_reason_enabled: bool = True, image_enabled: bool = False,
                 image_model: str | None = None, image_mode: str = "direct",
                 caption_prompt: str = "", fetcher=None, prefetcher=None, chat_context=None,
                 diagnostic_capture: bool = False):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be explicitly selected")
        if type(max_duration_seconds) is not int or max_duration_seconds <= 0:
            raise ValueError("invalid duration limit")
        if strategy not in ("model", "priority"):
            raise ValueError("invalid rule selection strategy")
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
        self.strategy = strategy
        self.no_action_reason_enabled = no_action_reason_enabled
        self.image_enabled = image_enabled
        self.image_model = image_model
        self.image_mode = image_mode
        self.caption_prompt = caption_prompt
        self.fetcher = fetcher
        self.prefetcher = prefetcher
        self.chat_context = chat_context
        self.diagnostic_capture = diagnostic_capture

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
        input_material = {"message": message, "rules": model_rules, "strategy": self.strategy,
                          "language": language, "prompt_version": PROMPT_VERSION}
        if caption:
            input_material["caption"] = caption
        if context_prompt is not None:
            input_material["chat_context"] = context_prompt
        if images:
            input_material["image_count"] = image_count
            input_material["image_bytes"] = image_bytes
        input_hash = _input_hash(input_material)
        start_detail = {"backend": "llm", "model": judge_model, "strategy": self.strategy, "rules": rule_data,
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
        started = time.monotonic()
        invoke_kwargs = {"model": judge_model, "message": message, "rules": model_rules,
                         "strategy": self.strategy, "language": language}
        if context_prompt is not None:
            invoke_kwargs["chat_context"] = context_prompt
        if images and self.image_mode == "direct":
            invoke_kwargs["image_urls"] = image_data_urls
        elif images and caption:
            invoke_kwargs["image_caption"] = caption
        response = await self.invoke(**invoke_kwargs)
        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        normalized = (normalize_priority_response(response) if self.strategy == "priority" else
                      normalize_response(response, allow_context_review=context is not None))
        detail = {"backend": "llm", "model": judge_model, "rules": rule_data,
                  "strategy": self.strategy, "prompt_language": language, "prompt_version": PROMPT_VERSION,
                  "response_status": normalized.reason, "model_latency_ms": elapsed_ms,
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
                    detail["rejected_call"] = _audit_rejected_call(
                        proposal_call, max_duration_seconds=self.max_duration_seconds)
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
                else:
                    detail["rejected_call"] = _audit_rejected_call(
                        normalized.calls[0], max_duration_seconds=self.max_duration_seconds)
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
