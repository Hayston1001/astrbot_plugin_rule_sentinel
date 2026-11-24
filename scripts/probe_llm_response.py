"""Offline response-shape probe using installed AstrBot and provider SDK types.

No model, provider client or network is used. This does not verify that a
provider preserves input or that an actual API returns complete metadata.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix="sentinel-response-probe-") as root:
        os.environ["ASTRBOT_ROOT"] = root
        from anthropic.types import Message
        from astrbot.core.provider.entities import LLMResponse
        from google.genai.types import Candidate, FinishReason, GenerateContentResponse
        from openai.types.chat.chat_completion import ChatCompletion, Choice
        from openai.types.responses import Response
        from sentinel.llm_protocol import normalize_assessment_response

        raw_results = [
            GenerateContentResponse(candidates=[Candidate(finish_reason=FinishReason.MAX_TOKENS)]),
            ChatCompletion.model_construct(choices=[Choice.model_construct(finish_reason="length")]),
            Response.model_construct(status="incomplete", incomplete_details=None),
            Message.model_construct(stop_reason="max_tokens"),
        ]
        for raw in raw_results:
            assessment = LLMResponse(role="tool", tools_call_name=["assess_rules"],
                tools_call_args=[{"assessments": []}], raw_completion=raw)
            text_only = LLMResponse(role="assistant", completion_text="done", raw_completion=raw)
            assert normalize_assessment_response(assessment).reason == "incomplete_model_output"
            assert normalize_assessment_response(text_only).reason == "incomplete_model_output"
        missing_finish = ChatCompletion.model_construct(choices=[Choice.model_construct(finish_reason=None)])
        assert normalize_assessment_response(LLMResponse(role="assistant", completion_text="done",
            raw_completion=missing_finish)).reason == "incomplete_model_output"
        lost_tools = ChatCompletion.model_construct(choices=[Choice.model_construct(finish_reason="tool_calls")])
        assert normalize_assessment_response(LLMResponse(role="assistant", completion_text="done",
            raw_completion=lost_tools)).reason == "inconsistent_finish_reason"
        complete = LLMResponse(role="tool", tools_call_name=["assess_rules"],
            tools_call_args=[{"assessments": [{"rule_id": "r", "outcome": "no_hit"}]}],
            raw_completion=GenerateContentResponse(candidates=[Candidate(finish_reason=FinishReason.STOP)]))
        assert normalize_assessment_response(complete).status == "assessments"
    print("Synthetic provider response shape checks passed; no model or network used.")


if __name__ == "__main__":
    main()
