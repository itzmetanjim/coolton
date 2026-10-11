"""One lock per key (a thread, a channel) that only exists while someone holds or
waits for it, so a long-running process doesn't keep a lock for every key it ever saw."""
from __future__ import annotations

import threading
from contextlib import contextmanager


class KeyedLocks:
    def __init__(self):
        self._guard = threading.Lock()
        self._locks: dict = {}  # key -> [RLock, how many hold or wait for it]

    @contextmanager
    def hold(self, key):
        """Hold `key`'s lock for the block (re-entrant for the same thread)."""
        with self._guard:
            entry = self._locks.setdefault(key, [threading.RLock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1] -= 1
                if not entry[1]:
                    self._locks.pop(key, None)

    def __len__(self) -> int:
        with self._guard:
            return len(self._locks)
