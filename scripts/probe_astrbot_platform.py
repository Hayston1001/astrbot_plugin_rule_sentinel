"""Offline probe of the installed AstrBot Platform queue and tap prototype.

This does not start a platform adapter, event pipeline, server or model. Run it
with an AstrBot-compatible Python environment and AstrBot on PYTHONPATH.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix="sentinel-platform-probe-") as root:
        os.environ["ASTRBOT_ROOT"] = root
        from astrbot.core.platform.platform import Platform
        from sentinel.platform_tap import PlatformTap

        class TestPlatform(Platform):
            async def run(self):
                pass

            def meta(self):
                return None

        async def probe():
            queue = asyncio.Queue(maxsize=1)
            platform = TestPlatform({}, queue)
            observed, failures = [], []
            tap = PlatformTap(platform, observed.append, lambda: failures.append(True))
            tap.install()
            event = object()
            platform.commit_event(event)
            assert observed == [event]
            try:
                platform.commit_event(object())
            except asyncio.QueueFull:
                pass
            else:
                raise AssertionError("original queue failure was suppressed")
            assert observed == [event] and queue.get_nowait() is event
            tap.close()
            assert "commit_event" not in vars(platform)

            def fail(event):
                raise ValueError("observer failure")

            tap = PlatformTap(platform, fail, lambda: failures.append(True))
            tap.install()
            platform.commit_event(event)
            assert queue.get_nowait() is event and failures == [True]
            tap.close()

        asyncio.run(probe())
    print("AstrBot Platform queue and observer probe passed; no adapter or pipeline started.")


if __name__ == "__main__":
    main()
