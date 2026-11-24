"""Offline dry-run integration using a guarded mock OpenAI SDK transport.

Synthetic data only. No QQ adapter, real provider, network, API key or
moderation action is used. This probe is not wired into the plugin runtime.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix="sentinel-guarded-dry-run-") as root:
        os.environ["ASTRBOT_ROOT"] = root
        import httpx
        from openai import AsyncOpenAI
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        from sentinel.astrbot_llm import make_tool_set
        from sentinel.dry_run_queue import DryRunQueue
        from sentinel.dry_run_worker import DryRunWorker
        from sentinel.guarded_openai_invoker import GuardedOpenAIInvoker, GuardedModelCallError
        from sentinel.llm_decider import LlmDecider
        from sentinel.openai_request_builder import build_openai_request
        from sentinel.openai_request_guard import SingleRequestGuard, make_guarded_transport
        from sentinel.provider_binding import ProviderBinding, ProviderBindingGuard
        from sentinel.rule_store import RuleStore

        async def probe():
            database = Path(root) / "state.sqlite"
            rules, queue = RuleStore(database), DryRunQueue(database)
            rules.initialize()
            queue.initialize()
            rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                       text="no spam", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                       severity_seconds={"low": 60}, modified_by="admin")
            for message_id in ("proposal", "normal", "tamper", "context"):
                queue.enqueue(platform_id="p", group_id="g", message_id=message_id, capacity=10,
                    payload={"text": "private " + message_id, "sender_id": "trigger-sender",
                             "session_origin": "p:GroupMessage:g"})
            delivered = []
            provider = object.__new__(ProviderOpenAIOfficial)
            provider.provider_config = {"id": "synthetic", "type": "openai", "key": ["synthetic"]}
            provider.model_name = "synthetic"
            endpoint = "https://example.invalid/v1/chat/completions"
            async def handler(http_request):
                body = json.loads(http_request.content)
                message = json.loads(body["messages"][1]["content"])["message_untrusted"]
                delivered.append(message)
                if message == "private changed":
                    provider.provider_config["key"] = ["changed-secret"]
                if message == "private context":
                    return httpx.Response(400, json={"error": {"code": "context_length_exceeded",
                                                               "message": "private context and synthetic secret"}})
                assert body["tools"][0]["function"]["name"] == "assess_rules"
                rule_ids = [rule["rule_id"] for rule in json.loads(body["messages"][1]["content"])["rules"]]
                matching = message in ("private proposal", "private priority", "private tie", "private confirm")
                assessments = [{"rule_id": rule_id, "outcome": "hit", "severity": "low", "reason": "spam pattern"}
                               if matching else {"rule_id": rule_id, "outcome": "no_hit"} for rule_id in rule_ids]
                output = {"role": "assistant", "content": None, "tool_calls": [{"id": "batch",
                    "type": "function", "function": {"name": "assess_rules",
                                                     "arguments": json.dumps({"assessments": assessments})}}]}
                reason = "tool_calls"
                return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
                    "model": "synthetic", "choices": [{"index": 0, "finish_reason": reason, "message": output}]})
            parser = object.__new__(ProviderOpenAIOfficial)
            parser.reasoning_key = "reasoning_content"
            async def parse(completion):
                return await parser._parse_openai_completion(completion, make_tool_set())
            guarded = GuardedOpenAIInvoker(provider_id="synthetic", resolved_model="synthetic", endpoint=endpoint,
                api_key="synthetic", transport_factory=lambda: httpx.MockTransport(handler),
                parse_response=parse, timeout_seconds=10)
            async def invoke(*, model, message, rules, language):
                assert language == "en"
                if message != "private tamper":
                    return await guarded(model=model, message=message, rules=rules)
                request = build_openai_request(model=model, message=message, rules=rules)
                guard = SingleRequestGuard(endpoint=endpoint, expected=request)
                transport = make_guarded_transport(delegate=httpx.MockTransport(handler), guard=guard, api_key="synthetic")
                http = httpx.AsyncClient(transport=transport)
                client = AsyncOpenAI(api_key="synthetic", base_url="https://example.invalid/v1", http_client=http, max_retries=0)
                try:
                    completion = await client.chat.completions.create(**request, extra_body={"tools": []})
                    return await parse(completion)
                finally:
                    await client.close()

            try:
                await guarded(model="changed", message="private", rules=[])
            except GuardedModelCallError:
                pass
            else:
                raise AssertionError("changed provider was accepted")

            closed = []
            class FailingTransport(httpx.MockTransport):
                async def aclose(self):
                    closed.append(True)
                    await super().aclose()
            async def failed_request(request):
                raise RuntimeError("private request and synthetic credential")
            failed = GuardedOpenAIInvoker(provider_id="synthetic", resolved_model="synthetic", endpoint=endpoint,
                api_key="synthetic", transport_factory=lambda: FailingTransport(failed_request),
                parse_response=parse, timeout_seconds=10)
            sample_rules = [{"rule_id": "r", "text": "rule", "allowed_actions": ["mute"],
                             "duration_mode": "strict", "severity_seconds": {"low": 60}}]
            try:
                await failed(model="synthetic", message="private", rules=sample_rules)
            except GuardedModelCallError as error:
                assert "private" not in str(error) and "synthetic" not in str(error)
            else:
                raise AssertionError("transport failure was not reported")
            assert closed == [True]
            async def timed_out(request):
                raise httpx.ReadTimeout("private timeout")
            timed = GuardedOpenAIInvoker(provider_id="synthetic", resolved_model="synthetic", endpoint=endpoint,
                api_key="synthetic", transport_factory=lambda: httpx.MockTransport(timed_out),
                parse_response=parse, timeout_seconds=10)
            try:
                await timed(model="synthetic", message="private", rules=sample_rules)
            except TimeoutError as error:
                assert "private" not in str(error)
            else:
                raise AssertionError("provider timeout was not classified")

            notifications = []
            worker = DryRunWorker(queue, LlmDecider(rules, invoke, model="synthetic",
                max_duration_seconds=300, queue=queue), concurrency=1, timeout_seconds=10, notify=notifications.append)
            assert await worker.drain_group("p", "g") == 4
            events = queue.list_events(platform_id="p", group_id="g")
            assert [(event["message_id"], event["status"]) for event in events] == [
                ("proposal", "started"), ("proposal", "proposed"), ("normal", "started"),
                ("normal", "no_action"), ("tamper", "started"), ("tamper", "failed"),
                ("context", "started"), ("context", "failed")]
            proposal = json.loads(events[1]["detail"])["proposal"]
            assert proposal["target_sender_id"] == "trigger-sender" and proposal["duration_seconds"] == 60
            tamper = json.loads(events[5]["detail"])
            assert tamper["reason_code"] == "decision_error" and tamper["prompt_language"] == "en"
            assert "private" not in events[5]["detail"]
            context_failure = json.loads(events[-1]["detail"])
            assert context_failure["reason"] == "context_length_exceeded" and context_failure["prompt_language"] == "en"
            assert delivered == ["private proposal", "private normal", "private context"]
            assert notifications == ["model_failed", "context_limit"]
            assert all("private proposal" not in event["detail"] and "private normal" not in event["detail"]
                       and "private tamper" not in event["detail"] and "private context" not in event["detail"]
                       for event in events)
            assert queue.claim("p", "g") is None

            for group, second_priority in (("priority", 1), ("tie", 3), ("confirm", 3)):
                for rule_id, priority in (("r", 3), ("s", second_priority)):
                    rules.save(platform_id="p", group_id=group, rule_id=rule_id, expected_version=0,
                        text="no spam", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                        severity_seconds={"low": 60}, modified_by="admin", priority=priority)
                queue.enqueue(platform_id="p", group_id=group, message_id=group, capacity=10,
                    payload={"text": "private " + group, "sender_id": "trigger-sender",
                             "session_origin": f"p:GroupMessage:{group}"})
            priority_worker = DryRunWorker(queue, LlmDecider(rules, invoke, model="synthetic",
                max_duration_seconds=300, queue=queue), concurrency=1, timeout_seconds=10)
            confirm_worker = DryRunWorker(queue, LlmDecider(rules, invoke, model="synthetic",
                max_duration_seconds=300, queue=queue, equal_priority_policy="confirm"),
                concurrency=1, timeout_seconds=10)
            assert await priority_worker.drain_group("p", "priority") == 1
            assert await priority_worker.drain_group("p", "tie") == 1
            assert await confirm_worker.drain_group("p", "confirm") == 1
            priority_events = queue.list_events(platform_id="p", group_id="priority")
            tie_events = queue.list_events(platform_id="p", group_id="tie")
            confirm_events = queue.list_events(platform_id="p", group_id="confirm")
            for events_for_group in (priority_events, tie_events, confirm_events):
                assert [event["status"] for event in events_for_group] == ["started", "proposed"]
            priority_detail = json.loads(priority_events[-1]["detail"])
            assert priority_detail["selected_rules"] == ["r"] and priority_detail["peer_tie"] is False
            assert priority_detail["proposal"]["rule_id"] == "r"
            tie_detail = json.loads(tie_events[-1]["detail"])
            assert tie_detail["selected_rules"] == ["r", "s"] and tie_detail["peer_tie"] is False
            assert tie_detail["selection_reason"] == "equal_priority_match"
            confirm_detail = json.loads(confirm_events[-1]["detail"])
            assert confirm_detail["selected_rules"] == ["r", "s"] and confirm_detail["peer_tie"] is True
            assert all("private priority" not in event["detail"] for event in priority_events)
            assert all("private tie" not in event["detail"] for event in tie_events)
            assert all("private confirm" not in event["detail"] for event in confirm_events)

            rules.save(platform_id="p", group_id="bound", rule_id="r", expected_version=0,
                text="no spam", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                severity_seconds={"low": 60}, modified_by="admin")
            queue.enqueue(platform_id="p", group_id="bound", message_id="changed", capacity=10,
                payload={"text": "private changed", "sender_id": "trigger-sender",
                         "session_origin": "p:GroupMessage:bound"})
            async def resolve(provider_id):
                return provider
            binding = await ProviderBinding.capture(resolve, "synthetic")
            decide = ProviderBindingGuard(LlmDecider(rules, invoke, model="synthetic",
                max_duration_seconds=300, queue=queue), binding)
            bound_worker = DryRunWorker(queue, decide, concurrency=1, timeout_seconds=10)
            assert await bound_worker.drain_group("p", "bound") == 1
            bound_events = queue.list_events(platform_id="p", group_id="bound")
            assert [event["status"] for event in bound_events] == ["started", "review"]
            assert json.loads(bound_events[-1]["detail"])["review_reason"] == "provider_binding_changed"
            assert "changed-secret" not in bound_events[-1]["detail"]

        asyncio.run(probe())
    print("Guarded synthetic dry-run passed; no real model, platform or network used.")


if __name__ == "__main__":
    main()
