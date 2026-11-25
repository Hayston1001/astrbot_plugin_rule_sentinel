"""Isolated AstrBot LLM assessment caller, not registered as a chat tool.

Uses a single model request instead of executing a tool loop: the returned
assess_rules call is data for the dry-run validator. AstrBot providers may
silently drop or shrink input when a request exceeds the context window, so the
model decision is treated as advisory: every hit is validated against the stored rule
version and the triggering sender, and the audit records the input size. That
makes a truncated input a visible quality problem instead of an actionable one.
"""

import json
from functools import partial

from .prompts import PROMPTS
from .group_settings import validate_reason_language


def tool_parameters(language, *, no_action_reason_enabled=True):
    if type(no_action_reason_enabled) is not bool:
        raise ValueError("invalid no-action reason setting")
    text = PROMPTS[language]
    properties = {key: {"type": "integer" if key == "duration_seconds" else "string", "description": text[key]}
                  for key in ("rule_id", "severity", "duration_seconds", "reason")}
    if not no_action_reason_enabled:
        properties["reason"]["description"] = text["reason_hit"]
    properties["outcome"] = {"type": "string", "enum": ["hit", "no_hit", "review"],
                             "description": text["outcome"]}
    return {"type": "object", "properties": {"assessments": {"type": "array", "description": text["assessments"],
        "items": {"type": "object", "properties": properties, "required": ["rule_id", "outcome"],
                  "additionalProperties": False}}}, "required": ["assessments"], "additionalProperties": False}


# English defaults remain available to the separate, unwired request prototype.
SYSTEM_PROMPT = PROMPTS["en"]["system"]
TOOL_PARAMETERS = tool_parameters("en")


def make_tool_set(language="en", *, no_action_reason_enabled=True):
    # Import only when invoked; plugin initialization stays inactive.
    from astrbot.core.agent.tool import FunctionTool, ToolSet
    if language not in PROMPTS:
        raise ValueError("unsupported prompt language")
    return ToolSet([FunctionTool(name="assess_rules", description=PROMPTS[language]["tool"],
                                 parameters=tool_parameters(language, no_action_reason_enabled=no_action_reason_enabled), handler=None)])


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

    async def __call__(self, *, model: str, message: str, rules: list,
                       language: str = "en", image_urls=None, image_caption=None, chat_context=None,
                       retry_reason: str | None = None):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be explicitly selected")
        if (not isinstance(message, str) or not isinstance(rules, list) or not rules
                or (not message.strip() and not image_urls and not image_caption)):
            raise ValueError("message and rules are required")
        if language not in PROMPTS:
            raise ValueError("unsupported prompt language")
        if retry_reason is not None and (not isinstance(retry_reason, str)
                                         or not retry_reason.strip() or len(retry_reason) > 64):
            raise ValueError("invalid retry reason")
        if image_urls:
            trigger = {"text": message, "image_count": len(image_urls)}
        elif image_caption:
            trigger = {"text": message, "image_caption": image_caption}
        else:
            trigger = message
        prompt_data = {"rules": rules, "message_untrusted": trigger}
        prompt_key = "system" if self.no_action_reason_enabled else "system_no_reason"
        system_prompt = PROMPTS[language][prompt_key]
        if self.reason_language:
            system_prompt += ("\nWrite every decision reason in the administrator-specified language: "
                              + json.dumps(self.reason_language, ensure_ascii=False) + ". Keep all required protocol markers unchanged.")
        if image_urls:
            system_prompt += "\n" + PROMPTS[language]["image_direct"]
        elif image_caption:
            system_prompt += "\n" + PROMPTS[language]["image_caption_note"]
        if chat_context is not None:
            prompt_data["prior_messages_untrusted"] = chat_context
            system_prompt += "\n" + PROMPTS[language]["chat_context"]
        if retry_reason is not None:
            system_prompt += "\n" + PROMPTS[language]["retry"].format(reason=retry_reason.strip())
        prompt = json.dumps(prompt_data, ensure_ascii=False)
        return await self.context.llm_generate(chat_provider_id=model, prompt=prompt,
                                               image_urls=list(image_urls) if image_urls else None,
                                               tools=self.tool_set_factory(language),
                                               system_prompt=system_prompt,
                                               contexts=[])

    async def caption(self, *, model: str, prompt: str, image_urls):
        """Ask the configured vision model for a bounded image description."""
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be explicitly selected")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("caption prompt is required")
        if not isinstance(image_urls, (list, tuple)) or not image_urls:
            raise ValueError("image references are required")
        return await self.context.llm_generate(chat_provider_id=model, prompt=prompt,
                                               image_urls=list(image_urls))
