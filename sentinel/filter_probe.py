"""Prototype for a non-waking AstrBot custom filter; not registered by plugin.

In 4.29.3 a filter returning False does not mark its handler as waking. This
prototype intentionally catches observer failures because the framework stops
the original event when a filter raises. It requires real lifecycle and session
isolation testing before use. The waking stage runs before core whitelist and
session checks, so a future integration must explicitly address that boundary.
"""


def observe_and_decline(event, observe, on_error) -> bool:
    try:
        observe(event)
    except BaseException:
        try:
            on_error()
        except BaseException:
            pass
    return False


def make_filter(custom_filter_base, binding):
    """Create an unregistered 4.29-style filter for isolated experiments.

    binding.observe must be synchronous and nonblocking. A filter executes
    before AstrBot's session and access stages, so this does not establish
    authorization or coverage for production.
    """
    class ProbeFilter(custom_filter_base):
        def filter(self, event, cfg):
            return observe_and_decline(event, binding.observe, binding.on_error)
    return ProbeFilter
