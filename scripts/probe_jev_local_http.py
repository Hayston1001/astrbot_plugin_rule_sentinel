"""Synthetic Jev dry-run through aiohttp and a loopback-only local server.

The test subclass points at the local server. Production transport still uses
its fixed HTTPS endpoint. No external service or real credentials are used.
"""

import asyncio
import gzip
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from sentinel.dry_run_queue import DryRunQueue
    from sentinel.dry_run_worker import DryRunWorker
    from sentinel.jev_decider import JevDecider
    from sentinel.jev_transport import JevTransport
    from sentinel.rule_store import RuleStore

    async def probe():
        with tempfile.TemporaryDirectory(prefix="sentinel-jev-http-probe-") as root:
            database = Path(root) / "state.sqlite"
            queue, rules = DryRunQueue(database), RuleStore(database)
            queue.initialize()
            rules.initialize()
            rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                       text="synthetic rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                       severity_seconds={"low": 60}, modified_by="admin")
            seen, redirects = [], []

            async def decide(request):
                payload = await request.json()
                text = payload["state"]["message_untrusted"]
                seen.append(text)
                assert request.headers["Authorization"] == "Bearer synthetic-key"
                assert request.headers["Accept-Encoding"] == "identity"
                assert payload["state"]["rules"][0]["text"] == "synthetic rule"
                events = queue.list_events(platform_id="p", group_id="g")
                assert any(event["message_id"] == text and event["status"] == "started" for event in events)
                answer = {"model": "synthetic", "answers": {"decision": {"type": "choice",
                    "choice": "r_hit_low_0", "confidence": 0.8,
                    "probabilities": {"no_action": 0.1, "review": 0.1, "r_hit_low_0": 0.8}}}}
                if text == "duplicate":
                    return web.Response(body=b'{"model":"synthetic","answers":{},"answers":{}}', content_type="application/json")
                if text == "compressed":
                    return web.Response(body=gzip.compress(b'{}'), headers={"Content-Encoding": "gzip"})
                if text == "context":
                    return web.json_response({"detail": {"code": "max_tokens_exceeded"}}, status=400)
                if text == "redirect":
                    raise web.HTTPTemporaryRedirect(location="/unexpected")
                return web.json_response(answer)

            async def unexpected(request):
                redirects.append(True)
                return web.json_response({})

            app = web.Application()
            app.router.add_post("/jev", decide)
            app.router.add_post("/unexpected", unexpected)
            server = TestServer(app, host="127.0.0.1")
            await server.start_server()
            try:
                class LoopbackTransport(JevTransport):
                    ENDPOINT = str(server.make_url("/jev"))

                async with aiohttp.ClientSession(trust_env=False) as session:
                    send = LoopbackTransport(session, api_key="synthetic-key", timeout_seconds=2)
                    worker = DryRunWorker(queue, JevDecider(rules, send, model="synthetic",
                        max_duration_seconds=300, queue=queue), concurrency=1, timeout_seconds=3)
                    for text in ("valid", "duplicate", "compressed", "context", "redirect"):
                        assert queue.enqueue(platform_id="p", group_id="g", message_id=text, capacity=10,
                            payload={"text": text, "sender_id": "sender"}) == "queued"
                    assert await worker.drain_group("p", "g") == 5
                events = queue.list_events(platform_id="p", group_id="g")
                latest = {event["message_id"]: event for event in events}
                assert {key: value["status"] for key, value in latest.items()} == {
                    "valid": "proposed", "duplicate": "failed", "compressed": "failed",
                    "context": "failed", "redirect": "failed"}
                assert latest["context"]["detail"] == "context_length_exceeded"
                assert seen == ["valid", "duplicate", "compressed", "context", "redirect"] and not redirects
                assert all("synthetic-key" not in event["detail"] for event in events)
            finally:
                await server.close()

    asyncio.run(probe())
    print("Jev loopback dry-run probe passed; no external service or credentials used.")


if __name__ == "__main__":
    main()
