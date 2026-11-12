import asyncio
import threading
import unittest

from sentinel.async_io import run_sync_safely


class AsyncIoTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_waits_for_thread_to_finish(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        def write():
            entered.set()
            release.wait(timeout=2)
            completed.set()
        task = asyncio.create_task(run_sync_safely(write))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(completed.is_set())
