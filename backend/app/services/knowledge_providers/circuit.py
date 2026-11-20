"""Thread-safe per-source circuit breaker for external knowledge providers."""

from __future__ import annotations

import threading
import time
from typing import Any


class SourceCircuitBreaker:
    """Trips after consecutive transient failures and re-opens after cooldown."""

    def __init__(self, failure_threshold: int = 3, cooldown_sec: float = 60.0) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_sec = max(0.0, float(cooldown_sec))
        self._lock = threading.Lock()
        self._failures: dict[Any, int] = {}
        self._opened_until: dict[Any, float] = {}

    def allow_request(self, key: Any) -> bool:
        now = time.monotonic()
        with self._lock:
            opened_until = self._opened_until.get(key)
            if opened_until is None:
                return True
            if now < opened_until:
                return False
            self._opened_until.pop(key, None)
            self._failures[key] = 0
            return True

    def record_success(self, key: Any) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._opened_until.pop(key, None)

    def record_failure(self, key: Any) -> bool:
        now = time.monotonic()
        with self._lock:
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            if count >= self.failure_threshold:
                self._opened_until[key] = now + self.cooldown_sec
                return True
            return False

    def reset(self, key: Any = None) -> None:
        with self._lock:
            if key is None:
                self._failures.clear()
                self._opened_until.clear()
            else:
                self._failures.pop(key, None)
                self._opened_until.pop(key, None)


knowledge_circuit_breaker = SourceCircuitBreaker()
