import tempfile
import unittest
import asyncio
import threading
import importlib.util
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from sentinel.dry_run_host import DryRunHost
from sentinel.dry_run_queue import DryRunQueue
from sentinel.instance_lock import InstanceLock
from sentinel.rule_store import RuleStore
from sentinel.settings_store import SettingsStore


class HostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.rules = RuleStore(self.directory / "state.sqlite")
        self.queue = DryRunQueue(self.directory / "state.sqlite")
        self.rules.initialize()
        self.queue.initialize()
        self.config = {"decision_backend": "llm", "llm_provider_id": "model"}
        async def session_gate(message):
            return True
        self.session_gate = session_gate

    async def test_host_has_exclusive_lock_and_shutdown_releases_it(self):
        path = self.directory / "lock"
        host = DryRunHost(context=object(), config=self.config, rules=self.rules, queue=self.queue, lock_path=path, session_gate=self.session_gate)
        await host.start()
        competitor = InstanceLock(path)
        self.assertFalse(competitor.acquire())
        await host.close()
        self.assertTrue(competitor.acquire())
        competitor.release()

    @unittest.skipUnless(importlib.util.find_spec("aiohttp"), "aiohttp unavailable")
    async def test_enabled_auto_mute_uses_only_injected_platform(self):
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=0, text="rule", enabled=True,
                        actions=[{"type": "mute", "mode": "auto", "duration_mode": "strict", "severity_seconds": {"low": 60}}], modified_by="admin")
        settings = SettingsStore(self.queue.path, self.directory / "backup")
        config = settings.load_or_migrate({})
        config.update(monitoring_groups=["p/g"], decision_backend="llm",
                      llm_provider_id="model", action_execution_enabled=True)
        settings.save(config)
        self.queue.enqueue(platform_id="p", group_id="g", message_id="m", capacity=10,
                           payload={"text": "message", "sender_id": "sender"})
        called = asyncio.Event()
        calls = []
        class FakeSession:
            async def close(self):
                pass
        class FakePlatform:
            async def verify_permission(self, platform, group):
                return True
            async def mute(self, platform, group, sender, expiry):
                calls.append((platform, group, sender, expiry))
                called.set()
                return {"outcome": "succeeded"}
        async def invoke(**kwargs):
            return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["propose_action"],
                tools_call_args=[{"violated": True, "rule_id": "r", "severity": "low", "reason": "rule"}])
        host = DryRunHost(context=object(), config=config, rules=self.rules, queue=self.queue, lock_path=self.directory / "lock")
        with patch("aiohttp.ClientSession", return_value=FakeSession()), patch("sentinel.qq_mute_platform.QQMutePlatform", return_value=FakePlatform()), patch("sentinel.runtime_factory.AstrBotLlmInvoker", return_value=invoke):
            await host.start()
            try:
                await asyncio.wait_for(called.wait(), 5)
            finally:
                await host.close()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:3], ("p", "g", "sender"))
        with closing(sqlite3.connect(self.queue.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM execution_intents").fetchone()[0], "succeeded")

    async def test_incomplete_notification_config_rejected_before_lock(self):
        path = self.directory / "lock"
        config = {**self.config, "notification_target": "p:FriendMessage:u"}
        host = DryRunHost(context=object(), config=config, rules=self.rules, queue=self.queue, lock_path=path, session_gate=self.session_gate)
        with self.assertRaises(ValueError):
            await host.start()
        self.assertFalse(path.exists())

    async def test_bad_jev_configuration_releases_lock(self):
        class Session:
            async def close(self):
                self.closed = True
        session = Session()
        path = self.directory / "lock"
        config = {"decision_backend": "jev", "jev_model": "m"}
        host = DryRunHost(context=object(), config=config, rules=self.rules, queue=self.queue,
                          lock_path=path, session_factory=lambda: session, session_gate=self.session_gate)
        with self.assertRaises(ValueError):
            await host.start()
        self.assertTrue(session.closed)
        competitor = InstanceLock(path)
        self.assertTrue(competitor.acquire())
        competitor.release()

    async def test_group_jev_override_creates_and_closes_session(self):
        class Session:
            closed = False
            async def close(self):
                self.closed = True
        session = Session()
        config = {**self.config, "jev_api_key": "key", "jev_provider": "typesafe",
                  "group_overrides": {"p/g": {"model": {"backend": "jev", "model": "jev-latest"}}}}
        host = DryRunHost(context=object(), config=config, rules=self.rules, queue=self.queue,
                          lock_path=self.directory / "group-jev.lock", session_factory=lambda: session,
                          session_gate=self.session_gate)
        await host.start()
        self.assertIs(host.session, session)
        await host.close()
        self.assertTrue(session.closed)

    async def test_cancelled_start_waits_for_lock_thread_and_releases(self):
        entered, proceed = threading.Event(), threading.Event()
        class SlowLock:
            released = False
            def acquire(self):
                entered.set()
                proceed.wait(timeout=2)
                return True
            def release(self):
                self.released = True
        host = DryRunHost(context=object(), config=self.config, rules=self.rules, queue=self.queue,
                          lock_path=self.directory / "lock", session_gate=self.session_gate)
        host.lock = SlowLock()
        task = asyncio.create_task(host.start())
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        proceed.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(host.lock.released)

    async def test_close_during_startup_keeps_lock_until_startup_stops(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowRuntime:
            def __init__(self):
                self.stopped = False
                self.closed = False
            async def start_after_exclusive_stop(self):
                entered.set()
                await release.wait()
            def stop_new_decisions(self):
                self.stopped = True
            async def close(self):
                self.closed = True
        runtime = SlowRuntime()
        path = self.directory / "lock"
        host = DryRunHost(context=object(), config=self.config, rules=self.rules, queue=self.queue,
                          lock_path=path, session_gate=self.session_gate)
        with patch("sentinel.dry_run_host.build_runtime", return_value=runtime):
            start = asyncio.create_task(host.start())
            await asyncio.wait_for(entered.wait(), 2)
            close = asyncio.create_task(host.close())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            competitor = InstanceLock(path)
            self.assertTrue(runtime.stopped)
            self.assertFalse(close.done())
            self.assertFalse(competitor.acquire())
            self.assertFalse(runtime.closed)
            release.set()
            with self.assertRaisesRegex(RuntimeError, "closed during startup"):
                await asyncio.wait_for(start, 2)
            await asyncio.wait_for(close, 2)
        self.assertTrue(runtime.closed)
        self.assertTrue(competitor.acquire())
        competitor.release()

    async def test_close_waits_for_real_runtime_recovery_thread(self):
        entered, release = threading.Event(), threading.Event()
        original = self.queue.recover_after_exclusive_start
        def slow_recovery():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("recovery test timed out")
            return original()
        path = self.directory / "lock"
        host = DryRunHost(context=object(), config=self.config, rules=self.rules, queue=self.queue,
                          lock_path=path, session_gate=self.session_gate)
        with patch.object(self.queue, "recover_after_exclusive_start", side_effect=slow_recovery):
            start = asyncio.create_task(host.start())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                close = asyncio.create_task(host.close())
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                competitor = InstanceLock(path)
                self.assertFalse(competitor.acquire())
                self.assertFalse(close.done())
            finally:
                release.set()
            with self.assertRaisesRegex(RuntimeError, "closed during startup"):
                await asyncio.wait_for(start, 3)
            await asyncio.wait_for(close, 3)
            self.assertTrue(competitor.acquire())
            competitor.release()

    async def test_shutdown_finishes_current_decision_and_preserves_pending(self):
        self.rules.save(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                        text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                        severity_seconds={"low": 10}, modified_by="admin")
        for message_id in ("1", "2"):
            self.queue.enqueue(platform_id="p", group_id="g", message_id=message_id, capacity=10,
                payload={"text": "private", "sender_id": "user", "session_origin": "p:GroupMessage:g"})
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        class Transport:
            def __init__(self, *args, **kwargs):
                pass
            async def __call__(self, request):
                calls.append(request)
                entered.set()
                await release.wait()
                return {"model": "m", "answers": {"decision": {"type": "choice", "choice": "no_action",
                    "confidence": 0.8, "probabilities": {"no_action": 0.8, "review": 0.1, "r_hit_low_0": 0.1}}}}
        class Session:
            async def close(self):
                pass
        config = {"monitoring_groups": ["p/g"], "decision_backend": "jev",
                  "jev_model": "m", "jev_api_key": "key"}
        host = DryRunHost(context=object(), config=config, rules=self.rules, queue=self.queue,
                          lock_path=self.directory / "lock", session_factory=Session, session_gate=self.session_gate)
        with patch("sentinel.runtime_factory.JevTransport", Transport):
            await host.start()
            await asyncio.wait_for(entered.wait(), timeout=2)
            stopped = asyncio.Event()
            original_stop = host.runtime.worker.request_stop
            def request_stop():
                original_stop()
                stopped.set()
            host.runtime.worker.request_stop = request_stop
            close = asyncio.create_task(host.close())
            await asyncio.wait_for(stopped.wait(), timeout=2)
            release.set()
            await asyncio.wait_for(close, timeout=2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.queue.claim("p", "g").message_id, "2")
