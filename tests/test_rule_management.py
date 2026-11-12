import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sentinel.rule_management import RuleManagement
from sentinel.rule_store import RuleStore, VersionConflict


class RuleManagementTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = RuleStore(Path(temp.name) / "state.sqlite")
        self.store.initialize()
        self.time = [100]
        self.authorized = True
        self.manager = RuleManagement(self.store, lambda actor, platform, group: self.authorized and actor == "admin",
                                      confirmation_seconds=30, clock=lambda: self.time[0])
        self.fields = dict(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                           text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                           severity_seconds={"low": 10})

    def test_confirm_persists_identity_and_rejects_replay(self):
        preview = self.manager.preview("admin", self.fields)
        self.assertEqual(self.manager.confirm("admin", preview).modified_by, "admin")
        with self.assertRaises(VersionConflict):
            self.manager.confirm("admin", preview)

    def test_confirmation_binds_actor_fields_and_authorization(self):
        preview = self.manager.preview("admin", self.fields)
        preview["fields"]["group_id"] = "other"
        with self.assertRaises(ValueError):
            self.manager.confirm("admin", preview)
        preview = self.manager.preview("admin", self.fields)
        with self.assertRaises(PermissionError):
            self.manager.confirm("other", preview)
        self.authorized = False
        with self.assertRaises(PermissionError):
            self.manager.confirm("admin", preview)

    def test_expired_preview_and_untrusted_modifier(self):
        preview = self.manager.preview("admin", self.fields)
        self.time[0] = 130
        with self.assertRaises(ValueError):
            self.manager.confirm("admin", preview)
        with self.assertRaises(ValueError):
            self.manager.preview("admin", {**self.fields, "modified_by": "other"})

    def test_concurrent_confirmation_only_saves_one_version(self):
        preview = self.manager.preview("admin", self.fields)
        def confirm(_):
            try:
                return self.manager.confirm("admin", preview).version
            except VersionConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(confirm, range(2)))
        self.assertCountEqual(results, [1, "conflict"])
        self.assertEqual(len(self.store.history("p", "g", "r")), 1)
