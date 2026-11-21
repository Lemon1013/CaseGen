"""In-process event broker for live task-generation previews.

Inherits the shared ring-buffer, snapshot, and TTL semantics from
``app.services.stream_broker.BaseStreamBroker`` while preserving all public
symbols and compatibility hooks used by routes and tests.
"""

from __future__ import annotations

import time  # noqa: F401 — preserved for tests monkeypatching task_stream.time.monotonic
from collections.abc import Iterable

from app.services.stream_broker import (
    BaseStreamBroker,
    StreamEvent as TaskStreamEvent,
    StreamPoll as TaskStreamPoll,
    _PREVIEW_TRUNCATED_MESSAGE,
    _TERMINAL_EVENTS,
    _StreamState as _TaskStreamState,
    build_sse_streaming_response,
    encode_sse,
)


class TaskStreamBroker(BaseStreamBroker[int]):
    """Thread-safe, bounded task stream state keyed by integer task id."""

    def task_ids(self) -> Iterable[int]:
        """Small inspection hook used by tests and operational diagnostics."""
        return self.keys()


task_stream = TaskStreamBroker()

__all__ = [
    "TaskStreamBroker",
    "TaskStreamEvent",
    "TaskStreamPoll",
    "_PREVIEW_TRUNCATED_MESSAGE",
    "_TERMINAL_EVENTS",
    "_TaskStreamState",
    "build_sse_streaming_response",
    "encode_sse",
    "task_stream",
]
