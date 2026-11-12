"""Advisory process lock for exclusive dry-run queue recovery."""

import os
from pathlib import Path


class InstanceLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._file = None

    def acquire(self) -> bool:
        if self._file is not None:
            raise RuntimeError("lock is already held")
        file = open(self.path, "a+b")
        try:
            if file.seek(0, os.SEEK_END) == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    file.close()
                    return False
            else:
                import fcntl
                try:
                    fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    file.close()
                    return False
            self._file = file
            return True
        except BaseException:
            file.close()
            raise

    def release(self):
        file = self._file
        if file is None:
            return
        try:
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()
            self._file = None
