"""Normalize untrusted AstrBot LLM tool proposals without executing tools."""

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class ToolResponse:
    status: str
    calls: list[dict]
    reason: str
    explanation: str | None = None
    diagnostics: dict | None = None


def _known_incomplete(response: object) -> bool:
    raw = getattr(response, "raw_completion", None)
    if raw is None:
        return False
    def field(value, name):
        return value.get(name) if isinstance(value, dict) else getattr(value, name, None)
    incomplete = {"length", "max_tokens", "max_output_tokens", "content_filter", "error", "aborted",
                  "incomplete", "failed", "cancelled", "canceled", "in_progress", "queued", "safety", "recitation", "blocklist",
                  "prohibited_content", "spii", "image_safety", "malformed_function_call",
                  "unexpected_tool_call", "model_context_window_exceeded", "other", "finish_reason_unspecified"}
    def normalize_reason(value):
        if isinstance(value, Enum):
            value = value.name
        return value.strip().split(".")[-1].casefold() if isinstance(value, str) else None
    def incomplete_reason(value):
        return normalize_reason(value) in incomplete
    if (incomplete_reason(field(raw, "stop_reason")) or incomplete_reason(field(raw, "finish_reason"))
            or incomplete_reason(field(raw, "status")) or field(raw, "incomplete_details") is not None):
        return True
    successful_reasons = {"stop", "tool_calls", "function_call", "end_turn", "tool_use", "stop_sequence"}
    for name in ("stop_reason", "finish_reason"):
        value = field(raw, name)
        if value is not None and normalize_reason(value) not in successful_reasons:
            return True
    status = field(raw, "status")
    if status is not None and normalize_reason(status) not in {"completed", "success", "succeeded", "ok"}:
        return True
    feedback = field(raw, "prompt_feedback")
    if feedback is not None and field(feedback, "block_reason") is not None:
        return True
    for field_name in ("choices", "candidates"):
        choices = field(raw, field_name)
        if choices is not None:
            if (not isinstance(choices, (list, tuple)) or len(choices) != 1
                    or normalize_reason(field(choices[0], "finish_reason")) not in successful_reasons):
                return True
    return False


def _openai_finish_conflicts(response, has_tools):
    raw = getattr(response, "raw_completion", None)
    choices = raw.get("choices") if isinstance(raw, dict) else getattr(raw, "choices", None)
    if not isinstance(choices, (list, tuple)) or len(choices) != 1:
        return False
    choice = choices[0]
    finish = choice.get("finish_reason") if isinstance(choice, dict) else getattr(choice, "finish_reason", None)
    if isinstance(finish, Enum):
        finish = finish.name
    finish = finish.strip().split(".")[-1].casefold() if isinstance(finish, str) else None
    return (finish in ("tool_calls", "function_call")) != has_tools


def _bounded_text(value, limit=64):
    if isinstance(value, Enum):
        value = value.name
    return value.strip()[:limit] if isinstance(value, str) else None


def _raw_field(raw, name):
    return raw.get(name) if isinstance(raw, dict) else getattr(raw, name, None)


def _diagnostics(response, names=None, args=None):
    """Describe untrusted tool output without keeping argument values."""
    result = {}
    if names is not None:
        if isinstance(names, list):
            result["tool_names"] = [str(name)[:64] for name in names[:5]]
            result["tool_name_count"] = len(names)
        else:
            result["tool_names_type"] = type(names).__name__
    if args is not None:
        if isinstance(args, list):
            result["argument_types"] = [type(item).__name__ for item in args[:5]]
            result["argument_count"] = len(args)
        else:
            result["arguments_type"] = type(args).__name__
    raw = getattr(response, "raw_completion", None)
    for key in ("finish_reason", "stop_reason", "status"):
        value = _bounded_text(_raw_field(raw, key))
        if value:
            result[key] = value
    if isinstance(args, list) and len(args) == 1 and isinstance(args[0], dict):
        result["argument_keys"] = sorted(str(key)[:64] for key in args[0])[:24]
    return result


def normalize_response(response: object, *, allow_context_review: bool = False) -> ToolResponse:
    """Accept at most one proposal from a completed assistant response.

    The caller must still apply rule, sender, duration and authorization checks.
    Error, streaming and malformed responses are never classified as no action.
    """
    role = getattr(response, "role", None)
    if role not in ("assistant", "tool") or getattr(response, "is_chunk", None) is not False:
        return ToolResponse("failed", [], "incomplete_or_failed_response", diagnostics=_diagnostics(response))
    if _known_incomplete(response):
        return ToolResponse("failed", [], "incomplete_model_output", diagnostics=_diagnostics(response))
    names = getattr(response, "tools_call_name", None)
    args = getattr(response, "tools_call_args", None)
    if not isinstance(names, list) or not isinstance(args, list) or len(names) != len(args):
        return ToolResponse("failed", [], "malformed_tool_calls",
                            diagnostics=_diagnostics(response, names, args))
    if _openai_finish_conflicts(response, bool(names)):
        return ToolResponse("failed", [], "inconsistent_finish_reason",
                            diagnostics=_diagnostics(response, names, args))
    if not names:
        if role != "assistant":
            return ToolResponse("failed", [], "empty_tool_response",
                                diagnostics=_diagnostics(response, names, args))
        text = getattr(response, "completion_text", None)
        if not isinstance(text, str):
            return ToolResponse("failed", [], "unconfirmed_no_action",
                                diagnostics=_diagnostics(response, names, args))
        lines = text.strip().split("\n", 1)
        if allow_context_review and lines == ["NEEDS_REVIEW"]:
            return ToolResponse("review", [], "context_evidence_insufficient")
        if lines[0].strip() != "NO_ACTION":
            return ToolResponse("failed", [], "unconfirmed_no_action",
                                diagnostics=_diagnostics(response, names, args))
        explanation = lines[1].strip() if len(lines) == 2 else None
        if explanation is not None and (not explanation or len(explanation) > 2000):
            return ToolResponse("failed", [], "invalid_no_action_reason",
                                diagnostics=_diagnostics(response, names, args))
        return ToolResponse("no_action", [], "explicit_no_action", explanation)
    if len(names) != 1:
        return ToolResponse("rejected", [], "multiple_tool_calls",
                            diagnostics=_diagnostics(response, names, args))
    if names[0] != "propose_action" or not isinstance(args[0], dict):
        return ToolResponse("rejected", [], "unsupported_tool_or_arguments",
                            diagnostics=_diagnostics(response, names, args))
    if args[0].get("violated") is not True or "action" in args[0]:
        return ToolResponse("rejected", [], "invalid_verdict",
                            diagnostics=_diagnostics(response, names, args))
    return ToolResponse("proposed", [args[0]], "proposal_received")


def normalize_priority_response(response: object) -> ToolResponse:
    """Require a single data-only batch assessment, not multiple tool calls."""
    if getattr(response, "role", None) not in ("assistant", "tool") or getattr(response, "is_chunk", None) is not False:
        return ToolResponse("failed", [], "incomplete_or_failed_response", diagnostics=_diagnostics(response))
    if _known_incomplete(response):
        return ToolResponse("failed", [], "incomplete_model_output", diagnostics=_diagnostics(response))
    names = getattr(response, "tools_call_name", None)
    args = getattr(response, "tools_call_args", None)
    if not isinstance(names, list) or not isinstance(args, list) or len(names) != len(args):
        return ToolResponse("failed", [], "malformed_tool_calls",
                            diagnostics=_diagnostics(response, names, args))
    if _openai_finish_conflicts(response, bool(names)):
        return ToolResponse("failed", [], "inconsistent_finish_reason",
                            diagnostics=_diagnostics(response, names, args))
    if len(names) != 1:
        return ToolResponse("rejected", [], "missing_or_multiple_assessments",
                            diagnostics=_diagnostics(response, names, args))
    if names[0] != "assess_rules" or not isinstance(args[0], dict) or set(args[0]) != {"assessments"}:
        return ToolResponse("rejected", [], "invalid_assessment_tool",
                            diagnostics=_diagnostics(response, names, args))
    assessments = args[0]["assessments"]
    if not isinstance(assessments, list) or any(not isinstance(item, dict) for item in assessments):
        return ToolResponse("rejected", [], "invalid_assessments",
                            diagnostics=_diagnostics(response, names, args))
    return ToolResponse("assessments", assessments, "assessment_received")
