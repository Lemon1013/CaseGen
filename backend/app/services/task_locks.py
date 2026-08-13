"""Process-local serialization for task action endpoints.

CaseGen currently runs generation and its live broker in one process. These
locks close check/transition/enqueue races between generate, regenerate,
review, optimize, and delete requests for the same task. Entries are removed
when the holder and all waiters leave, so arbitrary task ids cannot grow the
registry without bound.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class _LockEntry:
    lock: threading.RLock = field(default_factory=threading.RLock)
    users: int = 0


class TaskLockRegistry:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[int, _LockEntry] = {}

    @contextmanager
    def hold(self, task_id: int) -> Iterator[None]:
        with self._guard:
            entry = self._entries.get(task_id)
            if entry is None:
                entry = _LockEntry()
                self._entries[task_id] = entry
            entry.users += 1
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._guard:
                entry.users -= 1
                if entry.users == 0 and self._entries.get(task_id) is entry:
                    self._entries.pop(task_id, None)

    def size(self) -> int:
        with self._guard:
            return len(self._entries)


task_locks = TaskLockRegistry()
