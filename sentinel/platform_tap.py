"""Experimental, opt-in platform commit observer; not installed by the plugin.

AstrBot 4.29.3's normal event filter marks all matching messages as waking.
This prototype observes the platform commit boundary instead. It must be
reviewed and tested against actual platform lifecycle before enabling: it wraps
a non-public integration point and may coexist with wrappers from other plugins.
"""

from functools import wraps


class PlatformTap:
    def __init__(self, platform, observe, on_error):
        self.platform = platform
        self.observe = observe
        self.on_error = on_error
        self.wrapper = None
        self.original = None
        self.had_instance_attribute = False
        self._state = None

    def install(self):
        if self.wrapper is not None:
            raise RuntimeError("tap already installed")
        namespace = vars(self.platform)
        self.had_instance_attribute = "commit_event" in namespace
        self.original = getattr(self.platform, "commit_event")
        if not callable(self.original):
            raise TypeError("commit_event must be callable")
        original = self.original
        state = {"enabled": False}

        @wraps(original)
        def wrapper(event):
            # The original event is committed first. An observer failure is
            # isolated and cannot prevent or replace ordinary processing.
            result = original(event)
            if state["enabled"]:
                try:
                    self.observe(event)
                except BaseException:
                    try:
                        self.on_error()
                    except BaseException:
                        pass
            return result

        setattr(self.platform, "commit_event", wrapper)
        self.wrapper = wrapper
        self._state = state
        state["enabled"] = True

    def close(self):
        if self._state is not None:
            self._state["enabled"] = False
        if self.wrapper is None:
            return
        # Do not overwrite a wrapper another plugin installed after this tap.
        if vars(self.platform).get("commit_event") is self.wrapper:
            if self.had_instance_attribute:
                setattr(self.platform, "commit_event", self.original)
            else:
                delattr(self.platform, "commit_event")
        self.wrapper = None
        self._state = None
