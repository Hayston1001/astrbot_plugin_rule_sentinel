import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from sentinel.active_group_index import ActiveGroupIndex
from sentinel.dry_run_queue import DryRunQueue
from sentinel.management_service import ManagementService
from sentinel.rule_store import RuleStore, VersionConflict


class ActiveGroupIndexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "state.sqlite"
        self.store = RuleStore(path)
        self.store.initialize()
        self.queue = DryRunQueue(path)
        self.queue.initialize()
        self.index = ActiveGroupIndex(self.store)
        self.service = ManagementService(self.store, self.queue, group_index=self.index)
        self.fields = dict(platform_id="p", group_id="g", rule_id="r", expected_version=0,
                           text="rule", enabled=True, allowed_actions=["mute"], duration_mode="strict",
                           severity_seconds={"low": 10})

    def save(self, fields):
        return self.service.confirm("admin", self.service.preview("admin", fields))

    def test_latest_rule_versions_and_management_updates(self):
        self.assertTrue(self.index.reload())
        self.assertFalse(self.index.is_enabled("p", "g"))
        self.save(self.fields)
        self.assertTrue(self.index.is_enabled("p", "g"))
        self.save({**self.fields, "expected_version": 1, "enabled": False})
        self.assertFalse(self.index.is_enabled("p", "g"))
        self.assertEqual(self.store.enabled_groups(), set())
        self.assertTrue(self.index.reload())
        self.assertFalse(self.index.is_enabled("p", "g"))

    def test_failed_save_stays_disabled_until_a_later_success(self):
        self.save(self.fields)
        with self.assertRaises(VersionConflict):
            self.save(self.fields)
        self.assertFalse(self.index.is_enabled("p", "g"))
        self.index.reload()
        self.assertFalse(self.index.is_enabled("p", "g"))
        self.save({**self.fields, "expected_version": 1})
        self.assertTrue(self.index.is_enabled("p", "g"))

    def test_overlapping_saves_cannot_enable_group_early(self):
        self.save(self.fields)
        self.index.invalidate("p", "g")
        self.index.invalidate("p", "g")
        self.assertFalse(self.index.finish_save("p", "g", True))
        self.assertFalse(self.index.is_enabled("p", "g"))
        self.assertFalse(self.index.reload() and self.index.is_enabled("p", "g"))
        self.assertTrue(self.index.finish_save("p", "g", True))
        self.assertTrue(self.index.is_enabled("p", "g"))

    def test_refresh_failure_clears_existing_entry(self):
        self.save(self.fields)
        def fail(*args):
            raise OSError("database unavailable")
        self.store.current_for_group = fail
        with self.assertRaises(OSError):
            self.index.refresh_group("p", "g")
        self.assertFalse(self.index.is_enabled("p", "g"))

    def test_invalidation_during_refresh_cannot_restore_stale_data(self):
        started, resume = threading.Event(), threading.Event()
        class Store:
            def current_for_group(self, *args):
                started.set()
                if not resume.wait(5):
                    raise TimeoutError()
                return [SimpleNamespace(enabled=True)]
        index = ActiveGroupIndex(Store())
        result = []
        thread = threading.Thread(target=lambda: result.append(index.refresh_group("p", "g")))
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            index.invalidate("p", "g")
        finally:
            resume.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        self.assertFalse(index.is_enabled("p", "g"))

    def test_other_group_refresh_does_not_cancel_this_group(self):
        started, resume = threading.Event(), threading.Event()
        class Store:
            def current_for_group(self, platform, group):
                if group == "first":
                    started.set()
                    if not resume.wait(5):
                        raise TimeoutError()
                return [SimpleNamespace(enabled=True)]
        index = ActiveGroupIndex(Store())
        result = []
        thread = threading.Thread(target=lambda: result.append(index.refresh_group("p", "first")))
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            index.invalidate("p", "second")
            self.assertTrue(index.finish_save("p", "second", True))
        finally:
            resume.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [True])
        self.assertTrue(index.is_enabled("p", "first"))
        self.assertTrue(index.is_enabled("p", "second"))

    def test_reload_failure_invalidates_inflight_group_refresh(self):
        started, resume = threading.Event(), threading.Event()
        class Store:
            def current_for_group(self, *args):
                started.set()
                if not resume.wait(5):
                    raise TimeoutError()
                return [SimpleNamespace(enabled=True)]
            def enabled_groups(self):
                raise OSError("database unavailable")
        index = ActiveGroupIndex(Store())
        result = []
        thread = threading.Thread(target=lambda: result.append(index.refresh_group("p", "g")))
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            with self.assertRaises(OSError):
                index.reload()
        finally:
            resume.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        self.assertFalse(index.is_enabled("p", "g"))

    def test_refresh_cannot_reenable_group_during_failing_reload(self):
        started, resume = threading.Event(), threading.Event()
        class Store:
            def current_for_group(self, *args):
                return [SimpleNamespace(enabled=True)]
            def enabled_groups(self):
                started.set()
                if not resume.wait(5):
                    raise TimeoutError()
                raise OSError("database unavailable")
        index = ActiveGroupIndex(Store())
        failures = []
        def reload_index():
            try:
                index.reload()
            except OSError:
                failures.append(True)
        thread = threading.Thread(target=reload_index)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            self.assertFalse(index.refresh_group("p", "g"))
            self.assertFalse(index.is_enabled("p", "g"))
        finally:
            resume.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [True])
        self.assertFalse(index.is_enabled("p", "g"))

    def test_reload_failure_clears_index(self):
        self.save(self.fields)
        def fail():
            raise OSError("database unavailable")
        self.store.enabled_groups = fail
        with self.assertRaises(OSError):
            self.index.reload()
        self.assertFalse(self.index.is_enabled("p", "g"))
