import tempfile
import unittest
import subprocess
import sys
from pathlib import Path

from sentinel.instance_lock import InstanceLock


class InstanceLockTests(unittest.TestCase):
    def test_exclusive_and_reusable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "queue.lock"
            first, second = InstanceLock(path), InstanceLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_other_process_cannot_acquire_held_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "queue.lock"
            lock = InstanceLock(path)
            self.assertTrue(lock.acquire())
            try:
                code = "import sys; from sentinel.instance_lock import InstanceLock; x=InstanceLock(sys.argv[1]); print(int(x.acquire())); x.release()"
                result = subprocess.run([sys.executable, "-B", "-c", code, str(path)],
                                        cwd=Path(__file__).resolve().parent.parent,
                                        capture_output=True, text=True, timeout=10, check=True)
                self.assertEqual(result.stdout.strip(), "0")
            finally:
                lock.release()
