"""Build an independent OpenAI-compatible dry-run request snapshot.

Experimental only: this does not select a provider, send requests or execute
tools. The caller remains responsible for verified rules, sender identity,
audit, endpoint, authorization and the final transport boundary.
"""

import copy
import json

from .astrbot_llm import PRIORITY_SYSTEM_PROMPT, PRIORITY_TOOL_PARAMETERS, SYSTEM_PROMPT, TOOL_PARAMETERS
from .action_config import validate_actions
from .prompts import PROMPTS
from .dry_run_ingest import MAX_MESSAGE_CHARS
from .openai_request_guard import RequestBodyGuard, RequestIntegrityError


def build_openai_request(*, model: str, message: str, rules: list, strategy: str) -> dict:
    if (not isinstance(model, str) or not model.strip() or not isinstance(message, str) or not message.strip()
            or len(message) > MAX_MESSAGE_CHARS or not isinstance(rules, list) or not rules
            or strategy not in ("model", "priority")):
        raise RequestIntegrityError("invalid request inputs")
    rule_ids = set()
    for rule in rules:
        if isinstance(rule, dict) and set(rule) <= {"rule_id", "text", "duration_mode", "severity_levels"}:
            rule_id, text = rule.get("rule_id"), rule.get("text")
            mode, levels = rule.get("duration_mode"), rule.get("severity_levels")
            if (not isinstance(rule_id, str) or not rule_id.strip() or rule_id in rule_ids
                    or not isinstance(text, str) or not text.strip()
                    or (mode is None and levels is not None)
                    or (mode is not None and mode not in ("strict", "autonomous"))
                    or (mode == "strict" and (not isinstance(levels, list) or not levels
                        or any(not isinstance(level, str) or not level.strip() for level in levels)))
                    or (mode == "autonomous" and levels is not None)):
                raise RequestIntegrityError("invalid rule snapshot")
            rule_ids.add(rule_id)
            continue
        if isinstance(rule, dict) and set(rule) == {"rule_id", "text", "actions"}:
            if (not isinstance(rule["rule_id"], str) or not rule["rule_id"].strip() or rule["rule_id"] in rule_ids
                    or not isinstance(rule["text"], str) or not rule["text"].strip() or not isinstance(rule["actions"], list)):
                raise RequestIntegrityError("invalid rule snapshot")
            try:
                actions = []
                for action in rule["actions"]:
                    if not isinstance(action, dict) or "template" in action:
                        raise ValueError("invalid action")
                    actions.append({**action, "template": "placeholder"} if action.get("type") == "reply" else action)
                if not validate_actions(actions):
                    raise ValueError("missing action")
            except (TypeError, ValueError):
                raise RequestIntegrityError("invalid rule snapshot") from None
            rule_ids.add(rule["rule_id"])
            continue
        if (not isinstance(rule, dict)
                or set(rule) != {"rule_id", "text", "allowed_actions", "duration_mode", "severity_seconds"}
                or not isinstance(rule["rule_id"], str) or not rule["rule_id"].strip()
                or rule["rule_id"] in rule_ids or not isinstance(rule["text"], str) or not rule["text"].strip()
                or rule["allowed_actions"] != ["mute"] or rule["duration_mode"] not in ("strict", "autonomous")
                or not isinstance(rule["severity_seconds"], dict)):
            raise RequestIntegrityError("invalid rule snapshot")
        mapping = rule["severity_seconds"]
        if ((rule["duration_mode"] == "strict" and not mapping)
                or (rule["duration_mode"] == "autonomous" and mapping)
                or any(not isinstance(name, str) or not name.strip() or type(seconds) is not int or seconds <= 0
                       for name, seconds in mapping.items())):
            raise RequestIntegrityError("invalid rule snapshot")
        rule_ids.add(rule["rule_id"])
    try:
        prompt = json.dumps({"rules": rules, "message_untrusted": message}, ensure_ascii=False, allow_nan=False)
        name = "assess_rules" if strategy == "priority" else "propose_action"
        description = PROMPTS["en"]["tool_priority" if strategy == "priority" else "tool_model"]
        request = {"model": model, "messages": [
            {"role": "system", "content": PRIORITY_SYSTEM_PROMPT if strategy == "priority" else SYSTEM_PROMPT},
            {"role": "user", "content": prompt}],
            "tools": [{"type": "function", "function": {"name": name, "description": description,
                "parameters": copy.deepcopy(PRIORITY_TOOL_PARAMETERS if strategy == "priority" else TOOL_PARAMETERS)}}],
            "tool_choice": "auto", "stream": False}
        RequestBodyGuard(request)
        return request
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise RequestIntegrityError("invalid request inputs") from None
