"""Isolated AstrBot LLM proposal caller, not registered as a chat tool.

Uses a single model request instead of executing a tool loop: the returned tool
call is data for the dry-run validator. AstrBot providers may silently drop or
shrink input when a request exceeds the context window, so the model decision is
treated as advisory: every proposal is validated against the stored rule
version and the triggering sender, and the audit records the input size. That
makes a truncated input a visible quality problem instead of an actionable one.
"""

import json
from functools import partial

from .prompts import PROMPTS
from .group_settings import validate_reason_language


def tool_parameters(strategy, language, *, no_action_reason_enabled=True):
    if type(no_action_reason_enabled) is not bool:
        raise ValueError("invalid no-action reason setting")
    text = PROMPTS[language]
    properties = {key: {"type": "integer" if key == "duration_seconds" else "string", "description": text[key]}
                  for key in ("rule_id", "severity", "duration_seconds", "reason")}
    if not no_action_reason_enabled:
        properties["reason"]["description"] = text["reason_hit"]
    if strategy == "model":
        properties["violated"] = {"type": "boolean", "enum": [True]}
        return {"type": "object", "properties": properties, "required": ["rule_id", "violated", "reason"],
                "additionalProperties": False}
    if strategy != "priority":
        raise ValueError("invalid rule selection strategy")
    properties["outcome"] = {"type": "string", "enum": ["hit", "no_hit", "review"],
                             "description": text["outcome"]}
    return {"type": "object", "properties": {"assessments": {"type": "array", "description": text["assessments"],
        "items": {"type": "object", "properties": properties, "required": ["rule_id", "outcome"],
                  "additionalProperties": False}}}, "required": ["assessments"], "additionalProperties": False}


# English defaults remain available to the separate, unwired request prototype.
SYSTEM_PROMPT = PROMPTS["en"]["system_model"]
PRIORITY_SYSTEM_PROMPT = PROMPTS["en"]["system_priority"]
TOOL_PARAMETERS = tool_parameters("model", "en")
PRIORITY_TOOL_PARAMETERS = tool_parameters("priority", "en")


def make_tool_set(strategy="model", language="en", *, no_action_reason_enabled=True):
    # Import only when invoked; plugin initialization stays inactive.
    from astrbot.core.agent.tool import FunctionTool, ToolSet
    if language not in PROMPTS:
        raise ValueError("unsupported prompt language")
    if strategy == "priority":
        return ToolSet([FunctionTool(name="assess_rules", description=PROMPTS[language]["tool_priority"],
                                     parameters=tool_parameters(strategy, language, no_action_reason_enabled=no_action_reason_enabled), handler=None)])
    if strategy != "model":
        raise ValueError("invalid rule selection strategy")
    return ToolSet([FunctionTool(name="propose_action", description=PROMPTS[language]["tool_model"],
                                 parameters=tool_parameters(strategy, language, no_action_reason_enabled=no_action_reason_enabled), handler=None)])


class AstrBotLlmInvoker:
    def __init__(self, context, *, tool_set_factory=make_tool_set, reason_language="", no_action_reason_enabled=True):
        validate_reason_language(reason_language)
        if type(no_action_reason_enabled) is not bool:
            raise ValueError("invalid no-action reason setting")
        self.context = context
        self.tool_set_factory = (partial(make_tool_set, no_action_reason_enabled=no_action_reason_enabled)
                                 if tool_set_factory is make_tool_set else tool_set_factory)
        self.reason_language = reason_language.strip()
        self.no_action_reason_enabled = no_action_reason_enabled

    async def __call__(self, *, model: str, message: str, rules: list, strategy: str = "model", language: str = "en"):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be explicitly selected")
        if not isinstance(message, str) or not message.strip() or not isinstance(rules, list) or not rules:
            raise ValueError("message and rules are required")
        if strategy not in ("model", "priority"):
            raise ValueError("invalid rule selection strategy")
        if language not in PROMPTS:
            raise ValueError("unsupported prompt language")
        prompt = json.dumps({"rules": rules, "message_untrusted": message}, ensure_ascii=False)
        prompt_key = "system_" + strategy + ("" if self.no_action_reason_enabled else "_no_reason")
        system_prompt = PROMPTS[language][prompt_key]
        if self.reason_language:
            system_prompt += ("\nWrite every decision reason in the administrator-specified language: "
                              + json.dumps(self.reason_language, ensure_ascii=False) + ". Keep all required protocol markers unchanged.")
        return await self.context.llm_generate(chat_provider_id=model, prompt=prompt,
                                               tools=self.tool_set_factory(strategy, language),
                                               system_prompt=system_prompt,
                                               contexts=[])
