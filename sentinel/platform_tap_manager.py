"""Experimental lifecycle coordinator for explicit, non-production tap tests."""

from .platform_tap import PlatformTap


class PlatformTapManager:
    SUPPORTED = frozenset({"qq_official", "qq_official_webhook"})

    def __init__(self, observe, on_error):
        self.observe = observe
        self.on_error = on_error
        self.taps = {}

    def reconcile(self, platforms):
        """Attach to supported live instances and detach replaced instances."""
        desired = {}
        for platform in platforms:
            meta = platform.meta()
            if meta.name not in self.SUPPORTED:
                continue
            if not isinstance(meta.id, str) or not meta.id.strip() or meta.id in desired:
                raise ValueError("ambiguous platform instance")
            desired[meta.id] = platform
        added = {}
        try:
            for platform_id, platform in desired.items():
                existing = self.taps.get(platform_id)
                if existing is None or existing.platform is not platform:
                    tap = PlatformTap(platform, self.observe, self.on_error)
                    tap.install()
                    added[platform_id] = tap
        except Exception:
            for tap in added.values():
                tap.close()
            raise
        for platform_id in list(self.taps):
            if desired.get(platform_id) is not self.taps[platform_id].platform:
                self.taps.pop(platform_id).close()
        self.taps.update(added)

    def close(self):
        for tap in self.taps.values():
            tap.close()
        self.taps.clear()
