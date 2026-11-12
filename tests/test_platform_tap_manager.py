import unittest
from types import SimpleNamespace

from sentinel.platform_tap_manager import PlatformTapManager


class Platform:
    def __init__(self, identifier, name="qq_official"):
        self.identifier, self.name, self.events = identifier, name, []
    def meta(self):
        return SimpleNamespace(id=self.identifier, name=self.name)
    def commit_event(self, event):
        self.events.append(event)


class ManagerTests(unittest.TestCase):
    def test_reconcile_replacement_and_unsupported_platform(self):
        observed = []
        manager = PlatformTapManager(observed.append, lambda: None)
        first, other = Platform("id"), Platform("other", "telegram")
        manager.reconcile([first, other])
        first.commit_event("one")
        other.commit_event("ignored")
        replacement = Platform("id")
        manager.reconcile([replacement, other])
        first.commit_event("old")
        replacement.commit_event("new")
        manager.close()
        replacement.commit_event("after")
        self.assertEqual(observed, ["one", "new"])

    def test_duplicate_id_fails_before_mutating_taps(self):
        first, duplicate = Platform("id"), Platform("id")
        manager = PlatformTapManager(lambda event: None, lambda: None)
        with self.assertRaises(ValueError):
            manager.reconcile([first, duplicate])
        self.assertNotIn("commit_event", vars(first))
