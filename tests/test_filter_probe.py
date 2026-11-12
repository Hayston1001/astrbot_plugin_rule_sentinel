import unittest

from sentinel.filter_probe import make_filter, observe_and_decline


class FilterProbeTests(unittest.TestCase):
    def test_declines_without_mutating_original_event(self):
        event = {"wake": False, "stopped": False}
        seen = []
        self.assertFalse(observe_and_decline(event, seen.append, lambda: None))
        self.assertEqual(seen, [event])
        self.assertEqual(event, {"wake": False, "stopped": False})

    def test_observer_errors_are_never_raised_to_pipeline(self):
        def fail(event):
            raise RuntimeError("private")
        def fail_error_handler():
            raise RuntimeError("error handler")
        self.assertFalse(observe_and_decline({}, fail, fail_error_handler))

    def test_generated_filter_declines_wake(self):
        class Base:
            pass
        class Binding:
            def __init__(self):
                self.events = []
            def observe(self, event):
                self.events.append(event)
            def on_error(self):
                raise AssertionError("unexpected error")
        binding = Binding()
        probe = make_filter(Base, binding)()
        self.assertFalse(probe.filter("message", {}))
        self.assertEqual(binding.events, ["message"])
