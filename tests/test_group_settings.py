import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from sentinel.dry_run_queue import DryRunQueue
from sentinel.dry_run_worker import Decision
from sentinel.group_settings import decision_fingerprint, execution_allowed, resolve_group_settings
from sentinel.mute_execution_store import MuteExecutionStore
from sentinel.rule_store import RuleStore
from sentinel.runtime_factory import GroupDecider, build_runtime
from sentinel.settings_store import DEFAULTS, SettingsStore, validate_settings


class GroupSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "state.sqlite"
        self.queue, self.rules = DryRunQueue(self.path), RuleStore(self.path)
        self.queue.initialize()
        self.rules.initialize()
        self.store = SettingsStore(self.path, Path(temp.name) / "backup")
        self.config = self.store.load_or_migrate({})
        self.config.update(monitoring_groups=["p1/g", "p2/g"],
                           decision_backend="llm", llm_provider_id="global-model",
                           action_execution_enabled=True)
        self.store.save(self.config)

    def test_inheritance_override_and_platform_isolation(self):
        config = copy.deepcopy(self.config)
        config["group_overrides"] = {"p1/g": {"model": {"backend": "llm", "model": "local-model"},
                                               "strategy": "priority", "execution_mode": "record",
                                               "protected_policy": "group_message",
                                               "protected_template": "group {rule_id}", "protected_quote": True}}
        validate_settings(config)
        selected = resolve_group_settings(config, "p1", "g")
        other = resolve_group_settings(config, "p2", "g")
        self.assertEqual((selected["model"], selected["strategy"], selected["execution_mode"]),
                         ("local-model", "priority", "record"))
        self.assertEqual((other["model"], other["strategy"], other["execution_mode"]),
                         ("global-model", "model", "execute"))
        self.assertFalse(execution_allowed(config, "p1", "g"))
        self.assertTrue(execution_allowed(config, "p2", "g"))
        config["action_execution_enabled"] = False
        config["group_overrides"]["p1/g"]["execution_mode"] = "execute"
        self.assertFalse(execution_allowed(config, "p1", "g"))
        self.assertFalse(execution_allowed(config, "p2", "g"))

    def test_invalid_overrides_fail_closed(self):
        bad_values = [None, [], {"model": None}, {"model": {}},
                      {"model": {"backend": "llm", "model": " "}},
                      {"model": {"backend": "invalid", "model": "m"}},
                      {"model": {"backend": "jev", "model": "jev-latest"}},
                      {"strategy": "unknown"}, {"execution_mode": "unknown"},
                      {"protected_quote": 1}, {"protected_policy": "group_message", "protected_template": ""},
                      {"protected_template": "{secret}"}, {"llm_reason_language": None},
                      {"llm_reason_language": 1}, {"llm_reason_language": "x" * 81},
                      {"llm_reason_language": "English\nother"}, {"unexpected": True}]
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_settings({**self.config, "group_overrides": {"p1/g": value}})
        for key in ("p1", "p1/g/extra", " p/g", "__rule_sentinel_system__/g"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_settings({**self.config, "group_overrides": {key: {}}})

    def test_migration_preserves_existing_execution_and_independent_defaults(self):
        old = copy.deepcopy(self.config)
        old.pop("default_execution_mode")
        old.pop("group_overrides")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE dashboard_settings SET payload=?", (json.dumps(old),))
            db.commit()
        migrated = self.store.load_or_migrate({})
        self.assertTrue(migrated["action_execution_enabled"])
        self.assertEqual(migrated["default_execution_mode"], "execute")
        self.assertEqual(migrated["group_overrides"], {})
        migrated["group_overrides"]["p1/g"] = {"strategy": "priority"}
        self.assertEqual(DEFAULTS["group_overrides"], {})
        self.assertEqual(self.store.load_or_migrate({})["group_overrides"], {})

    def test_router_uses_actual_group_model_and_strategy(self):
        config = copy.deepcopy(self.config)
        config["group_overrides"] = {"p1/g": {"model": {"backend": "llm", "model": "local-model"},
                                               "strategy": "priority"}}
        routes = []
        def make(backend, model, strategy, reason_language):
            routes.append((backend, model, strategy, reason_language))
            async def decide(item):
                return Decision("no_action", "{}")
            return decide
        router = GroupDecider(config, make)
        async def run():
            return [await router(SimpleNamespace(platform_id=p, group_id="g")) for p in ("p1", "p2", "p1")]
        results = asyncio.run(run())
        self.assertEqual(routes, [("llm", "local-model", "priority", ""), ("llm", "global-model", "model", "")])
        self.assertEqual(results[0].settings_fingerprint, decision_fingerprint(config, "p1", "g"))
        self.assertNotEqual(results[0].settings_fingerprint, results[1].settings_fingerprint)
        runtime = build_runtime(context=object(), config=config, rules=self.rules, queue=self.queue)
        self.assertEqual(runtime.worker.decide.decide.make_decider("llm", "local-model", "priority").current().strategy, "priority")
        self.assertEqual(runtime.worker.decide.decide.make_decider("llm", "global-model", "model").current().model, "global-model")

    def test_reason_language_changes_reach_runtime_and_invalidate_old_decisions(self):
        config = copy.deepcopy(self.config)
        config["llm_reason_language"] = "Japanese"
        validate_settings(config)
        runtime = build_runtime(context=object(), config=config, rules=self.rules, queue=self.queue)
        decider = runtime.worker.decide.decide.make_decider("llm", "global-model", "model").current()
        self.assertEqual(decider.invoke.reason_language, "Japanese")
        self.assertNotEqual(decision_fingerprint(config, "p1", "g"), decision_fingerprint(self.config, "p1", "g"))
        self.assertTrue(runtime.admission.settings_match())
        config["llm_reason_language"] = "English"
        self.assertFalse(runtime.admission.settings_match())
        for invalid in (None, 1, "x" * 81, "English\nother"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_settings({**self.config, "llm_reason_language": invalid})

    def test_no_action_reason_switch_changes_decider_without_invalidating_settings(self):
        config = copy.deepcopy(self.config)
        config["llm_no_action_reason_enabled"] = False
        runtime = build_runtime(context=object(), config=config, rules=self.rules, queue=self.queue)
        router = runtime.worker.decide.decide.make_decider("llm", "global-model", "model")
        decider = router.current()
        self.assertFalse(decider.no_action_reason_enabled)
        self.assertFalse(decider.invoke.no_action_reason_enabled)
        self.assertEqual(decision_fingerprint(config, "p1", "g"), decision_fingerprint(self.config, "p1", "g"))
        self.assertTrue(runtime.admission.settings_match())
        config["llm_no_action_reason_enabled"] = True
        self.assertTrue(runtime.admission.settings_match())
        self.assertTrue(router.current().no_action_reason_enabled)
        self.assertTrue(router.current().invoke.no_action_reason_enabled)
        config["llm_no_action_reason_enabled"] = False
        self.assertIs(router.current(), decider)
        config["llm_no_action_reason_enabled"] = 1
        with self.assertRaises(ValueError):
            runtime.admission.settings_match()

    def test_group_reason_language_inherits_or_overrides_including_empty(self):
        config = copy.deepcopy(self.config)
        config["llm_reason_language"] = "中文"
        config["group_overrides"] = {"p1/g": {"llm_reason_language": "English"},
                                     "p2/g": {"llm_reason_language": ""}}
        validate_settings(config)
        self.assertEqual(resolve_group_settings(config, "p1", "g")["llm_reason_language"], "English")
        self.assertEqual(resolve_group_settings(config, "p2", "g")["llm_reason_language"], "")
        self.assertEqual(resolve_group_settings(config, "p3", "g")["llm_reason_language"], "中文")
        self.assertEqual(self.store.load_or_migrate({})["group_overrides"], {})
        self.store.save(config)
        self.assertEqual(self.store.load_or_migrate({})["group_overrides"], config["group_overrides"])

    def test_group_reason_language_reaches_actual_prompt_per_scope(self):
        config = copy.deepcopy(self.config)
        config["llm_reason_language"] = "Japanese"
        config["monitoring_groups"].append("p1/other")
        config["group_overrides"] = {"p1/g": {"llm_reason_language": "English"},
                                     "p1/other": {"llm_reason_language": ""}}
        self.store.save(config)
        scopes = [("p1", "g"), ("p2", "g"), ("p1", "other")]
        for platform, group in scopes:
            self.rules.save(platform_id=platform, group_id=group, rule_id="r", expected_version=0,
                            text="No spam", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                            severity_seconds={"low": 60}, modified_by="admin")
            self.queue.enqueue(platform_id=platform, group_id=group, message_id="m", capacity=10,
                               payload={"text": f"message {platform} {group}", "sender_id": "sender"})
        calls = {}
        class Context:
            async def llm_generate(self, **kwargs):
                message = json.loads(kwargs["prompt"])["message_untrusted"]
                calls[message] = kwargs["system_prompt"]
                return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=[], tools_call_args=[],
                                       completion_text="NO_ACTION\nNo violation.")
        runtime = build_runtime(context=Context(), config=config, rules=self.rules, queue=self.queue)
        router = runtime.worker.decide.decide
        original = router.make_decider
        def make(*route):
            decider = original(*route)
            decider.current().invoke.tool_set_factory = lambda *_: None
            return decider
        router.make_decider = make
        async def run():
            return await asyncio.gather(*(runtime.worker.drain_group(*scope) for scope in scopes))
        self.assertEqual(asyncio.run(run()), [1, 1, 1])
        self.assertIn('administrator-specified language: "English"', calls["message p1 g"])
        self.assertIn('administrator-specified language: "Japanese"', calls["message p2 g"])
        self.assertNotIn("administrator-specified language", calls["message p1 other"])
        self.assertEqual(len(router.deciders), 3)

    def test_disabled_no_action_setting_reaches_both_runtime_strategies(self):
        config = copy.deepcopy(self.config)
        config["llm_no_action_reason_enabled"] = False
        config["group_overrides"] = {"p2/g": {"strategy": "priority"}}
        self.store.save(config)
        for platform in ("p1", "p2"):
            self.rules.save(platform_id=platform, group_id="g", rule_id="r", expected_version=0,
                            text="No spam", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                            severity_seconds={"low": 60}, modified_by="admin")
            self.queue.enqueue(platform_id=platform, group_id="g", message_id="m", capacity=10,
                               payload={"text": f"message {platform}", "sender_id": "sender"})
        calls = {}
        class Context:
            async def llm_generate(self, **kwargs):
                message = json.loads(kwargs["prompt"])["message_untrusted"]
                calls[message] = kwargs["system_prompt"]
                if message == "message p1":
                    return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=[], tools_call_args=[],
                                           completion_text="NO_ACTION\nUnrequested reason")
                return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["assess_rules"],
                    tools_call_args=[{"assessments": [{"rule_id": "r", "outcome": "no_hit", "reason": "Unrequested reason"}]}])
        runtime = build_runtime(context=Context(), config=config, rules=self.rules, queue=self.queue)
        router = runtime.worker.decide.decide
        original = router.make_decider
        def make(*route):
            decider = original(*route)
            decider.current().invoke.tool_set_factory = lambda *_: None
            return decider
        router.make_decider = make
        async def run():
            return await asyncio.gather(*(runtime.worker.drain_group(platform, "g") for platform in ("p1", "p2")))
        self.assertEqual(asyncio.run(run()), [1, 1])
        self.assertIn("Output only NO_ACTION", calls["message p1"])
        self.assertIn("omit the reason for no_hit", calls["message p2"])
        for platform in ("p1", "p2"):
            event = self.queue.list_events(platform_id=platform, group_id="g")[-1]
            self.assertEqual(event["status"], "no_action")
            detail = json.loads(event["detail"])
            self.assertFalse(detail["no_action_reason_enabled"])
            self.assertNotIn("reason", detail)
            self.assertNotIn("Unrequested reason", event["detail"])

    def test_reason_switch_is_snapshotted_for_each_ongoing_and_later_decision(self):
        config = copy.deepcopy(self.config)
        config["group_overrides"] = {"p2/g": {"strategy": "priority"}}
        self.store.save(config)
        for platform in ("p1", "p2"):
            self.rules.save(platform_id=platform, group_id="g", rule_id="r", expected_version=0,
                            text="No spam", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                            severity_seconds={"low": 60}, modified_by="admin")
            for name in ("first", "second"):
                self.queue.enqueue(platform_id=platform, group_id="g", message_id=name, capacity=10,
                                   payload={"text": f"message {platform} {name}", "sender_id": "sender"})
        calls = {}
        async def run():
            release = asyncio.Event()
            started = {platform: asyncio.Event() for platform in ("p1", "p2")}
            class Context:
                async def llm_generate(self, **kwargs):
                    message = json.loads(kwargs["prompt"])["message_untrusted"]
                    calls[message] = kwargs["system_prompt"]
                    platform = message.split()[1]
                    if message.endswith("first"):
                        started[platform].set()
                        await release.wait()
                    if platform == "p1":
                        return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=[], tools_call_args=[],
                                               completion_text="NO_ACTION\nNo violation.")
                    return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["assess_rules"],
                        tools_call_args=[{"assessments": [{"rule_id": "r", "outcome": "no_hit", "reason": "No violation."}]}])
            runtime = build_runtime(context=Context(), config=config, rules=self.rules, queue=self.queue)
            router = runtime.worker.decide.decide
            original = router.make_decider
            def make(*route):
                decider = original(*route)
                for enabled in (False, True):
                    decider.for_setting(enabled).invoke.tool_set_factory = lambda *_: None
                return decider
            router.make_decider = make
            tasks = [asyncio.create_task(runtime.worker.drain_group(platform, "g")) for platform in ("p1", "p2")]
            try:
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), timeout=10)
                await asyncio.to_thread(self.store.save, {**config, "llm_no_action_reason_enabled": False})
                config["llm_no_action_reason_enabled"] = False
                self.assertTrue(runtime.admission.settings_match())
            finally:
                release.set()
            self.assertEqual(await asyncio.gather(*tasks), [2, 2])
            await asyncio.to_thread(self.store.save, {**config, "llm_no_action_reason_enabled": True})
            config["llm_no_action_reason_enabled"] = True
            for platform in ("p1", "p2"):
                await asyncio.to_thread(self.queue.enqueue, platform_id=platform, group_id="g", message_id="third",
                                        capacity=10, payload={"text": f"message {platform} third", "sender_id": "sender"})
            self.assertEqual(await asyncio.gather(*(runtime.worker.drain_group(platform, "g") for platform in ("p1", "p2"))), [1, 1])
        asyncio.run(run())
        self.assertIn("a concise reason on the next line", calls["message p1 first"])
        self.assertIn("Output only NO_ACTION", calls["message p1 second"])
        self.assertIn("a concise reason on the next line", calls["message p1 third"])
        self.assertIn("reason for each hit or no_hit", calls["message p2 first"])
        self.assertIn("omit the reason for no_hit", calls["message p2 second"])
        self.assertIn("reason for each hit or no_hit", calls["message p2 third"])
        for platform in ("p1", "p2"):
            events = [event for event in self.queue.list_events(platform_id=platform, group_id="g") if event["status"] != "started"]
            self.assertEqual([event["status"] for event in events], ["no_action"] * 3)
            details = [json.loads(event["detail"]) for event in events]
            self.assertEqual([detail["no_action_reason_enabled"] for detail in details], [True, False, True])
            self.assertIn("No violation.", details[0]["reason"])
            self.assertNotIn("reason", details[1])
            self.assertIn("No violation.", details[2]["reason"])

    def test_reason_change_during_violation_preserves_reason_and_confirmable_action(self):
        config = copy.deepcopy(self.config)
        self.rules.save(platform_id="p1", group_id="g", rule_id="r", expected_version=0,
                        text="No spam", enabled=True, modified_by="admin", actions=[{
                            "type": "mute", "mode": "confirm", "duration_mode": "strict", "severity_seconds": {"low": 60}}])
        self.queue.enqueue(platform_id="p1", group_id="g", message_id="first", capacity=10,
                           payload={"text": "first message", "sender_id": "sender"})
        async def run():
            started, release = asyncio.Event(), asyncio.Event()
            class Context:
                async def llm_generate(self, **kwargs):
                    message = json.loads(kwargs["prompt"])["message_untrusted"]
                    if message == "first message":
                        started.set()
                        await release.wait()
                    return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["propose_action"],
                        tools_call_args=[{"rule_id": "r", "violated": True, "severity": "low", "reason": "Violates the spam rule."}])
            runtime = build_runtime(context=Context(), config=config, rules=self.rules, queue=self.queue)
            router = runtime.worker.decide.decide
            original = router.make_decider
            def make(*route):
                decider = original(*route)
                for enabled in (False, True):
                    decider.for_setting(enabled).invoke.tool_set_factory = lambda *_: None
                return decider
            router.make_decider = make
            task = asyncio.create_task(runtime.worker.drain_group("p1", "g"))
            try:
                await asyncio.wait_for(started.wait(), timeout=10)
                await asyncio.to_thread(self.store.save, {**config, "llm_no_action_reason_enabled": False})
                config["llm_no_action_reason_enabled"] = False
            finally:
                release.set()
            self.assertEqual(await asyncio.wait_for(task, timeout=10), 1)
            await asyncio.to_thread(self.queue.enqueue, platform_id="p1", group_id="g", message_id="second", capacity=10,
                                    payload={"text": "second message", "sender_id": "sender"})
            self.assertEqual(await runtime.worker.drain_group("p1", "g"), 1)
        asyncio.run(run())
        events = [event for event in self.queue.list_events(platform_id="p1", group_id="g") if event["status"] != "started"]
        self.assertEqual([event["status"] for event in events], ["proposed", "proposed"])
        for event in events:
            self.assertEqual(json.loads(event["detail"])["proposal"]["reason"], "Violates the spam rule.")
        with closing(sqlite3.connect(self.path)) as db:
            intents = db.execute("SELECT id, status FROM execution_intents ORDER BY id").fetchall()
        self.assertEqual([status for _, status in intents], ["pending_confirm", "pending_confirm"])
        store = MuteExecutionStore(self.path)
        for intent_id, _ in intents:
            self.assertIsNotNone(store.begin_attempt(intent_id, requested_mode="confirm"))

    def enqueue_proposal(self, platform, message):
        self.rules.save(platform_id=platform, group_id="g", rule_id="r", expected_version=0,
                        text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                        severity_seconds={"low": 60}, modified_by="admin")
        self.queue.enqueue(platform_id=platform, group_id="g", message_id=message,
                           payload={"text": "private", "sender_id": "sender"}, capacity=10)
        item = self.queue.claim(platform, "g")
        detail = json.dumps({"proposal": {"action": "mute", "rule_id": "r", "rule_version": 1,
                                        "target_sender_id": "sender", "duration_seconds": 60, "mode": "auto"}})
        return item, detail

    def test_changed_configuration_during_decision_requires_review(self):
        item, detail = self.enqueue_proposal("p1", "m")
        fingerprint = decision_fingerprint(self.config, "p1", "g")
        changed = copy.deepcopy(self.config)
        changed["group_overrides"] = {"p1/g": {"strategy": "priority"}}
        self.store.save(changed)
        self.queue.finish(item, status="proposed", detail=detail, rule_versions=(("r", 1),),
                          settings_fingerprint=fingerprint)
        with closing(sqlite3.connect(self.path)) as db:
            event = db.execute("SELECT status, detail FROM dry_run_events ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(event[0], "review")
            self.assertEqual(json.loads(event[1])["review_reason"], "group_settings_changed_at_commit")
            self.assertEqual(db.execute("SELECT count(*) FROM execution_intents").fetchone()[0], 0)

    def test_only_affected_pending_intents_are_invalidated(self):
        for platform in ("p1", "p2"):
            item, detail = self.enqueue_proposal(platform, "m")
            self.queue.finish(item, status="proposed", detail=detail, rule_versions=(("r", 1),),
                              settings_fingerprint=decision_fingerprint(self.config, platform, "g"))
        changed = copy.deepcopy(self.config)
        changed["group_overrides"] = {"p1/g": {"model": {"backend": "llm", "model": "different"}}}
        self.store.save(changed)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT platform_id, status FROM execution_intents ORDER BY platform_id").fetchall(),
                             [("p1", "inactive"), ("p2", "pending_auto")])
        self.store.save(self.config)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM execution_intents WHERE platform_id='p1'").fetchone()[0], "inactive")

    def test_language_change_only_invalidates_groups_whose_effective_value_changes(self):
        config = copy.deepcopy(self.config)
        config["llm_reason_language"] = "Chinese"
        config["group_overrides"] = {"p1/g": {"llm_reason_language": "English"}}
        self.store.save(config)
        for platform in ("p1", "p2"):
            item, detail = self.enqueue_proposal(platform, "m")
            self.queue.finish(item, status="proposed", detail=detail, rule_versions=(("r", 1),),
                              settings_fingerprint=decision_fingerprint(config, platform, "g"))
        changed = copy.deepcopy(config)
        changed["llm_reason_language"] = "French"
        self.store.save(changed)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT platform_id, status FROM execution_intents ORDER BY platform_id").fetchall(),
                             [("p1", "pending_auto"), ("p2", "inactive")])
        changed["group_overrides"]["p1/g"]["llm_reason_language"] = "Japanese"
        self.store.save(changed)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM execution_intents WHERE platform_id='p1'").fetchone()[0], "inactive")

    def test_group_language_change_during_decision_requires_review(self):
        item, detail = self.enqueue_proposal("p1", "m")
        old = decision_fingerprint(self.config, "p1", "g")
        changed = copy.deepcopy(self.config)
        changed["group_overrides"] = {"p1/g": {"llm_reason_language": "English"}}
        self.store.save(changed)
        self.queue.finish(item, status="proposed", detail=detail, rule_versions=(("r", 1),), settings_fingerprint=old)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM dry_run_events ORDER BY id DESC LIMIT 1").fetchone()[0], "review")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM execution_intents").fetchone()[0], 0)

    def test_no_action_reason_change_preserves_pending_intents(self):
        config = copy.deepcopy(self.config)
        config["monitoring_groups"] += ["p3/g", "p4/g", "p5/g", "p6/g"]
        config["group_overrides"] = {"p2/g": {"model": {"backend": "llm", "model": "local-model"}}}
        self.store.save(config)
        for platform, mode in (("p1", "auto"), ("p2", "auto"), ("p3", "confirm"),
                               ("p4", "auto"), ("p5", "auto"), ("p6", "auto")):
            self.rules.save(platform_id=platform, group_id="g", rule_id="r", expected_version=0,
                            text="rule", enabled=True, modified_by="admin", actions=[{
                                "type": "mute", "mode": mode, "duration_mode": "strict", "severity_seconds": {"low": 60}}])
            self.queue.enqueue(platform_id=platform, group_id="g", message_id="m", capacity=10,
                               payload={"text": "private", "sender_id": "sender"})
            item = self.queue.claim(platform, "g")
            detail = json.dumps({"proposal": {"action": "mute", "rule_id": "r", "rule_version": 1,
                                               "target_sender_id": "sender", "duration_seconds": 60, "mode": mode}})
            self.queue.finish(item, status="proposed", detail=detail, rule_versions=(("r", 1),),
                              settings_fingerprint=decision_fingerprint(config, platform, "g"))
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE execution_intents SET status='pending_retry' WHERE platform_id='p4'")
            db.execute("UPDATE execution_intents SET status='executing' WHERE platform_id='p5'")
            db.execute("UPDATE execution_intents SET status='inactive' WHERE platform_id='p6'")
            db.commit()
            before = db.execute("SELECT platform_id, status, updated_at FROM execution_intents ORDER BY platform_id").fetchall()
            confirm_id = db.execute("SELECT id FROM execution_intents WHERE platform_id='p3'").fetchone()[0]
        self.assertEqual([(platform, status) for platform, status, _ in before],
                         [("p1", "pending_auto"), ("p2", "pending_auto"), ("p3", "pending_confirm"),
                          ("p4", "pending_retry"), ("p5", "executing"), ("p6", "inactive")])
        for enabled in (False, True):
            self.store.save({**config, "llm_no_action_reason_enabled": enabled})
            with closing(sqlite3.connect(self.path)) as db:
                self.assertEqual(db.execute("SELECT platform_id, status, updated_at FROM execution_intents ORDER BY platform_id").fetchall(), before)
        self.assertIsNotNone(MuteExecutionStore(self.path).begin_attempt(confirm_id, requested_mode="confirm"))
        self.store.save({**config, "llm_no_action_reason_enabled": False, "max_duration_seconds": 120})
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT platform_id, status FROM execution_intents ORDER BY platform_id").fetchall(),
                             [("p1", "inactive"), ("p2", "inactive"), ("p3", "executing"),
                              ("p4", "inactive"), ("p5", "executing"), ("p6", "inactive")])

    def test_no_action_reason_change_during_decision_preserves_results(self):
        no_action, _ = self.enqueue_proposal("p1", "m")
        proposal, detail = self.enqueue_proposal("p2", "m")
        fingerprints = {platform: decision_fingerprint(self.config, platform, "g") for platform in ("p1", "p2")}
        self.store.save({**self.config, "llm_no_action_reason_enabled": False})
        self.queue.finish(no_action, status="no_action", detail="{}", rule_versions=(("r", 1),),
                          settings_fingerprint=fingerprints["p1"])
        self.queue.finish(proposal, status="proposed", detail=detail, rule_versions=(("r", 1),),
                          settings_fingerprint=fingerprints["p2"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT platform_id, status FROM dry_run_events ORDER BY platform_id").fetchall(),
                             [("p1", "no_action"), ("p2", "proposed")])
            self.assertEqual(db.execute("SELECT platform_id, status FROM execution_intents").fetchall(),
                             [("p2", "pending_auto")])

    def test_group_routes_reach_deciders_and_record_correct_execution_mode(self):
        config = copy.deepcopy(self.config)
        config["group_overrides"] = {"p1/g": {"model": {"backend": "llm", "model": "local-model"},
                                               "execution_mode": "record"}}
        self.store.save(config)
        for platform in ("p1", "p2"):
            self.rules.save(platform_id=platform, group_id="g", rule_id="r", expected_version=0,
                            text="No spam", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                            severity_seconds={"low": 60}, modified_by="admin")
            self.queue.enqueue(platform_id=platform, group_id="g", message_id="m", capacity=10,
                               payload={"text": "private", "sender_id": "sender"})
        runtime = build_runtime(context=object(), config=config, rules=self.rules, queue=self.queue)
        router = runtime.worker.decide.decide
        original = router.make_decider
        seen = []
        async def llm(**kwargs):
            seen.append(("llm", kwargs["model"], kwargs["strategy"]))
            return SimpleNamespace(role="assistant", is_chunk=False, tools_call_name=["propose_action"],
                tools_call_args=[{"violated": True, "rule_id": "r", "severity": "low", "reason": "spam"}])
        def make(backend, model, strategy, reason_language):
            decider = original(backend, model, strategy, reason_language)
            decider.current().invoke = llm
            return decider
        router.make_decider = make
        async def run():
            return await asyncio.gather(runtime.worker.drain_group("p1", "g"), runtime.worker.drain_group("p2", "g"))
        self.assertEqual(asyncio.run(run()), [1, 1])
        self.assertCountEqual(seen, [("llm", "global-model", "model"), ("llm", "local-model", "model")])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT platform_id, status FROM execution_intents ORDER BY platform_id").fetchall(),
                             [("p1", "inactive"), ("p2", "pending_auto")])
