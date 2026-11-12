"""Nonblocking, fail-closed prefilter for groups with enabled rules.

This is an optimization to minimize what enters the in-memory ingress buffer,
not an authorization source. The admission layer must still re-read rules and
framework permissions. Failed refreshes leave the affected group disabled.
"""

import threading


class ActiveGroupIndex:
    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()
        self._groups = set()
        self._epoch = 0
        self._reload_epoch = 0
        self._reloading = False
        self._group_epochs = {}
        self._pending = {}
        self._failed = set()

    def is_enabled(self, platform_id: str, group_id: str) -> bool:
        with self._lock:
            return (platform_id, group_id) in self._groups

    def invalidate(self, platform_id: str, group_id: str):
        with self._lock:
            key = (platform_id, group_id)
            self._epoch += 1
            self._group_epochs[key] = self._group_epochs.get(key, 0) + 1
            self._groups.discard(key)
            if key not in self._pending:
                self._failed.discard(key)
            self._pending[key] = self._pending.get(key, 0) + 1

    def finish_save(self, platform_id: str, group_id: str, success: bool):
        key = (platform_id, group_id)
        with self._lock:
            if key not in self._pending:
                raise ValueError("save was not started")
            self._epoch += 1
            self._group_epochs[key] = self._group_epochs.get(key, 0) + 1
            if not success:
                self._failed.add(key)
            count = self._pending[key] - 1
            if count:
                self._pending[key] = count
                return False
            del self._pending[key]
            if key in self._failed:
                return False
        return self.refresh_group(platform_id, group_id)

    def reload(self) -> bool:
        with self._lock:
            if self._reloading:
                return False
            self._reloading = True
            self._epoch += 1
            self._reload_epoch += 1
            self._groups.clear()
            epoch = self._epoch
        try:
            groups = self.store.enabled_groups()
        except BaseException:
            with self._lock:
                self._reloading = False
            raise
        with self._lock:
            self._reloading = False
            if self._epoch != epoch:
                return False
            self._groups = set(groups) - set(self._pending) - self._failed
            return True

    def refresh_group(self, platform_id: str, group_id: str) -> bool:
        key = (platform_id, group_id)
        with self._lock:
            self._epoch += 1
            self._groups.discard(key)
            if self._reloading or key in self._pending or key in self._failed:
                return False
            self._group_epochs[key] = self._group_epochs.get(key, 0) + 1
            epoch = self._group_epochs[key]
            reload_epoch = self._reload_epoch
        rules = self.store.current_for_group(platform_id, group_id)
        enabled = any(rule.enabled for rule in rules)
        with self._lock:
            if self._group_epochs.get(key) != epoch or self._reload_epoch != reload_epoch:
                return False
            if enabled:
                self._groups.add(key)
            else:
                self._groups.discard(key)
            return True
