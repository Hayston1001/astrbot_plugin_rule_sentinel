import unittest
import asyncio

from sentinel.platform_tap import PlatformTap


class Platform:
    def __init__(self):
        self.events = []
    def commit_event(self, event):
        self.events.append(event)
        return "original result"


class TapTests(unittest.TestCase):
    def test_original_runs_first_and_event_is_not_modified(self):
        platform = Platform()
        observed = []
        event = {"wake": False}
        def observe(value):
            self.assertEqual(platform.events, [event])
            observed.append(value)
        tap = PlatformTap(platform, observe, lambda: None)
        tap.install()
        self.assertEqual(platform.commit_event(event), "original result")
        self.assertEqual(event, {"wake": False})
        self.assertEqual(observed, [event])
        tap.close()
        self.assertNotIn("commit_event", vars(platform))

    def test_observer_exception_does_not_block_original(self):
        platform = Platform()
        errors = []
        def observe(event):
            raise ValueError("private data")
        tap = PlatformTap(platform, observe, lambda: errors.append("failure"))
        tap.install()
        self.assertEqual(platform.commit_event("msg"), "original result")
        self.assertEqual(platform.events, ["msg"])
        self.assertEqual(errors, ["failure"])
        tap.close()

    def test_observer_cancellation_does_not_interrupt_original(self):
        platform = Platform()
        def observe(event):
            raise asyncio.CancelledError()
        tap = PlatformTap(platform, observe, lambda: None)
        tap.install()
        self.assertEqual(platform.commit_event("msg"), "original result")
        self.assertEqual(platform.events, ["msg"])
        tap.close()

    def test_later_wrapper_is_not_overwritten_on_close(self):
        platform = Platform()
        observed = []
        tap = PlatformTap(platform, observed.append, lambda: None)
        tap.install()
        previous = platform.commit_event
        def other(event):
            return previous(event)
        platform.commit_event = other
        tap.close()
        self.assertIs(platform.commit_event, other)
        platform.commit_event("msg")
        self.assertEqual(observed, [])
        self.assertEqual(platform.events, ["msg"])
        tap.install()
        platform.commit_event("again")
        self.assertEqual(observed, ["again"])
        tap.close()
