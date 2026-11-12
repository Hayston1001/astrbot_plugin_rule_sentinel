import tempfile
import unittest
import copy
from pathlib import Path

from sentinel.dry_run_queue import DryRunQueue
from sentinel.management_service import ManagementService
from sentinel.rule_store import RuleStore
from sentinel.settings_store import DEFAULTS


class ManagementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "state.sqlite"
        self.rules = RuleStore(path)
        self.rules.initialize()
        self.queue = DryRunQueue(path)
        self.queue.initialize()
        self.service = ManagementService(self.rules, self.queue)
        self.fields = dict(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                           text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                           severity_seconds={"low": 10})

    def test_dashboard_user_is_allowed_but_api_key_and_anonymous_are_denied(self):
        for actor in (None, "", " ", "api_key:123", " api_key:123"):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                self.service.preview(actor, self.fields)
        self.assertEqual(self.service.list_rules("admin", "p", "g"), [])
        self.assertEqual(self.service.list_rules("member", "p", "g"), [])
        self.assertEqual(self.service.preview("member", self.fields)["fields"]["rule_id"], "r")

    def test_confirmation_rechecks_authorization(self):
        preview = self.service.preview("admin", self.fields)
        with self.assertRaises(PermissionError):
            self.service.confirm("api_key:123", preview)
        self.assertEqual(self.rules.history("p", "g", "r"), [])

    def test_authorized_rule_and_audit_operations(self):
        saved = self.service.confirm("admin", self.service.preview("admin", self.fields))
        self.assertEqual(saved["modified_by"], "admin")
        self.assertEqual(len(self.service.list_rules("admin", "p", "g")), 1)
        self.assertEqual(len(self.service.rule_history("admin", "p", "g", "r")), 1)
        self.assertEqual(self.service.list_audit("admin", "p", "g"), [])
        self.queue.record_notification("model_failed", "unknown")
        self.assertEqual(self.service.list_notifications("admin")[0]["status"], "unknown")
        with self.assertRaises(PermissionError):
            self.service.list_notifications("api_key:key")

    def test_duration_limit_is_checked_at_preview_and_confirmation(self):
        limit = [100]
        service = ManagementService(self.rules, self.queue, duration_limit=lambda: limit[0])
        with self.assertRaises(ValueError):
            service.preview("admin", {**self.fields, "severity_seconds": {"low": 101}})
        preview = service.preview("admin", {**self.fields, "severity_seconds": {"low": 90}})
        limit[0] = 80
        with self.assertRaises(ValueError):
            service.confirm("admin", preview)
        self.assertEqual(self.rules.history("p", "g", "r"), [])

    def test_quick_start_completion_checks_saved_settings_and_known_groups(self):
        config = copy.deepcopy(DEFAULTS)
        for actor in (None, "", "api_key:123"):
            with self.subTest(actor=actor), self.assertRaises(PermissionError):
                self.service.complete_quick_start(actor, config)
            with self.assertRaises(PermissionError):
                self.service.quick_start_completed(actor)
        self.assertFalse(self.service.quick_start_completed("admin"))
        self.assertEqual(self.service.complete_quick_start("admin", config), {"completed": False})
        config["llm_provider_id"] = "model-a"
        self.service.confirm("admin", self.service.preview("admin", self.fields))
        self.assertEqual(self.service.complete_quick_start("admin", config), {"completed": False})
        config["monitoring_mode"] = "blocklist"
        config["monitoring_groups"] = ["p/g"]
        self.assertEqual(self.service.complete_quick_start("admin", config), {"completed": False})
        config["monitoring_groups"] = []
        self.assertEqual(self.service.complete_quick_start("admin", config), {"completed": True})
        self.assertTrue(ManagementService(self.rules, self.queue).quick_start_completed("other"))
        config["llm_provider_id"] = ""
        self.assertEqual(self.service.complete_quick_start("admin", config), {"completed": True})
