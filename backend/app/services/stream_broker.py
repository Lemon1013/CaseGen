"""Shared in-process SSE stream broker and StreamingResponse helper.

Both the generation task stream (``task_stream``) and the agent chat stream
(``agent_stream``) share identical ring-buffer retention, monotonic stream
identities, late-subscriber snapshot recovery, TTL eviction, and SSE framing.
Keeping that infrastructure in one module avoids drift between the two planes.

Note: this broker lives in process memory and is designed for single-process
deployments.  Multi-worker deployments would require a shared broker (e.g.
Redis).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

from fastapi.responses import StreamingResponse


KeyT = TypeVar("KeyT")

_TERMINAL_EVENTS = frozenset({"completed", "failed"})
_PREVIEW_TRUNCATED_MESSAGE = (
    "实时预览已达到内存上限，后续内容暂不展示；生成完成后会加载完整草稿。"
)
DEFAULT_SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Accel-Buffering": "no",
}


@dataclass(frozen=True)
class StreamEvent:
    sequence: int
    event: str
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event": self.event,
            **self.payload,
        }


@dataclass(frozen=True)
class StreamPoll:
    """Result of reading a stream after a subscriber sequence."""

    kind: Literal["events", "snapshot", "missing", "timeout"]
    events: tuple[StreamEvent, ...] = ()
    snapshot: dict[str, Any] | None = None


@dataclass
class _StreamState:
    stream_id: int = 0
    sequence: int = 0
    text: str = ""
    status: str | None = None
    terminal: str | None = None
    updated_at: float = field(default_factory=time.monotonic)
    expires_at: float | None = None
    events: deque[StreamEvent] = field(default_factory=deque)
    event_chars: int = 0
    truncated: bool = False
    truncation_notice_sent: bool = False


class BaseStreamBroker(Generic[KeyT]):
    """Thread-safe, bounded stream state with late-subscriber snapshots."""

    def __init__(
        self,
        *,
        max_tasks: int = 128,
        max_events_per_task: int = 512,
        max_preview_chars: int = 256_000,
        max_event_text_chars: int = 16_384,
        max_event_history_chars: int = 65_536,
        max_message_chars: int = 1_000,
        terminal_ttl_sec: float = 120.0,
    ) -> None:
        self.max_tasks = max(1, int(max_tasks))
        self.max_events_per_task = max(8, int(max_events_per_task))
        self.max_preview_chars = max(1, int(max_preview_chars))
        self.max_event_text_chars = max(1, int(max_event_text_chars))
        self.max_event_history_chars = max(1, int(max_event_history_chars))
        self.max_message_chars = max(1, int(max_message_chars))
        self.terminal_ttl_sec = max(1.0, float(terminal_ttl_sec))
        self._states: OrderedDict[KeyT, _StreamState] = OrderedDict()
        self._condition = threading.Condition(threading.RLock())
        self._next_stream_id = 1

    def _purge_expired_locked(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        expired = [
            key
            for key, state in self._states.items()
            if state.expires_at is not None and state.expires_at <= current
        ]
        for key in expired:
            self._states.pop(key, None)
        if expired:
            self._condition.notify_all()

    def _get_or_create_locked(self, key: KeyT) -> _StreamState:
        self._purge_expired_locked()
        state = self._states.get(key)
        if state is not None:
            self._states.move_to_end(key)
            return state
        while len(self._states) >= self.max_tasks:
            self._states.popitem(last=False)
            # Wake a subscriber whose state was evicted so it can close rather
            # than heartbeat forever with a sequence that no longer exists.
            self._condition.notify_all()
        state = _StreamState(stream_id=self._next_stream_id)
        self._next_stream_id += 1
        self._states[key] = state
        return state

    def discard(self, key: KeyT) -> None:
        """Discard retained state for a key and wake any subscribers."""
        with self._condition:
            self._purge_expired_locked()
            self._states.pop(key, None)
            self._condition.notify_all()

    def _bounded_message(self, message: str) -> str:
        return str(message or "")[: self.max_message_chars]

    def _snapshot_locked(self, state: _StreamState) -> dict[str, Any]:
        result: dict[str, Any] = {
            "stream_id": state.stream_id,
            "sequence": state.sequence,
            "status": state.status,
            "text": state.text,
            "terminal": state.terminal,
            "truncated": state.truncated,
        }
        if state.truncated:
            result["message"] = _PREVIEW_TRUNCATED_MESSAGE
        return result

    def _publish_truncation_notice_locked(
        self,
        key: KeyT,
        state: _StreamState,
    ) -> None:
        if state.truncation_notice_sent:
            return
        state.truncation_notice_sent = True
        self._append_locked(
            key,
            state,
            "notice",
            {"message": _PREVIEW_TRUNCATED_MESSAGE, "truncated": True},
        )

    def _append_locked(
        self,
        key: KeyT,
        state: _StreamState,
        event: str,
        payload: dict[str, Any],
    ) -> StreamEvent:
        state.sequence += 1
        state.updated_at = time.monotonic()
        item = StreamEvent(state.sequence, event, payload)
        state.events.append(item)
        state.event_chars += self._event_payload_chars(payload)
        while (
            len(state.events) > self.max_events_per_task
            or state.event_chars > self.max_event_history_chars
        ):
            removed = state.events.popleft()
            state.event_chars -= self._event_payload_chars(removed.payload)
        self._states.move_to_end(key)
        self._condition.notify_all()
        return item

    @staticmethod
    def _event_payload_chars(payload: dict[str, Any]) -> int:
        """Conservative character accounting for bounded history retention."""
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def start(self, key: KeyT, *, status: str, message: str = "") -> None:
        """Start a new run and discard any prior live preview."""
        with self._condition:
            state = self._get_or_create_locked(key)
            state.text = ""
            state.status = status
            state.terminal = None
            state.expires_at = None
            state.truncated = False
            state.truncation_notice_sent = False
            self._append_locked(
                key,
                state,
                "reset",
                {
                    "status": status,
                    "message": self._bounded_message(message),
                    "text": "",
                },
            )
            self._append_locked(
                key,
                state,
                "status",
                {"status": status, "message": self._bounded_message(message)},
            )

    def ensure(self, key: KeyT, *, status: str, terminal: str | None = None) -> None:
        """Create a snapshot state for an entity that predates the live broker."""
        with self._condition:
            if key in self._states:
                self._states.move_to_end(key)
                return
            state = self._get_or_create_locked(key)
            state.status = status
            if terminal in _TERMINAL_EVENTS:
                state.terminal = terminal
                state.expires_at = time.monotonic() + self.terminal_ttl_sec

    def status(self, key: KeyT, *, status: str, message: str = "") -> None:
        with self._condition:
            state = self._get_or_create_locked(key)
            state.status = status
            self._append_locked(
                key,
                state,
                "status",
                {"status": status, "message": self._bounded_message(message)},
            )

    def delta(self, key: KeyT, text: str) -> None:
        if not text:
            return
        with self._condition:
            state = self._get_or_create_locked(key)
            if state.terminal is not None:
                return
            if state.truncated:
                return

            remaining = self.max_preview_chars - len(state.text)
            accepted = text[:remaining]
            for offset in range(0, len(accepted), self.max_event_text_chars):
                chunk = accepted[offset : offset + self.max_event_text_chars]
                state.text += chunk
                self._append_locked(key, state, "delta", {"delta": chunk})

            if len(text) > len(accepted):
                state.truncated = True
                self._publish_truncation_notice_locked(key, state)

    def reset(self, key: KeyT, *, message: str = "", status: str | None = None) -> None:
        with self._condition:
            state = self._get_or_create_locked(key)
            state.text = ""
            state.truncated = False
            state.truncation_notice_sent = False
            if status is not None:
                state.status = status
            self._append_locked(
                key,
                state,
                "reset",
                {
                    "status": state.status,
                    "message": self._bounded_message(message),
                    "text": "",
                },
            )

    def retry(self, key: KeyT, *, message: str, attempt: int | None = None) -> None:
        payload: dict[str, Any] = {"message": self._bounded_message(message)}
        if attempt is not None:
            payload["attempt"] = attempt
        with self._condition:
            state = self._get_or_create_locked(key)
            self._append_locked(key, state, "retry", payload)

    def notice(self, key: KeyT, *, message: str) -> None:
        with self._condition:
            state = self._get_or_create_locked(key)
            self._append_locked(
                key,
                state,
                "notice",
                {"message": self._bounded_message(message)},
            )

    def complete(
        self,
        key: KeyT,
        *,
        text: str,
        status: str = "generated",
        message: str = "生成完成",
    ) -> None:
        with self._condition:
            state = self._get_or_create_locked(key)
            state.text = text[: self.max_preview_chars]
            state.truncated = len(text) > len(state.text)
            if state.truncated:
                self._publish_truncation_notice_locked(key, state)
            state.status = status
            state.terminal = "completed"
            state.expires_at = time.monotonic() + self.terminal_ttl_sec
            terminal_payload: dict[str, Any] = {
                "status": status,
                "message": self._bounded_message(
                    _PREVIEW_TRUNCATED_MESSAGE if state.truncated else message
                ),
                "truncated": state.truncated,
            }
            if len(state.text) <= self.max_event_text_chars:
                terminal_payload["text"] = state.text
            self._append_locked(
                key,
                state,
                "completed",
                terminal_payload,
            )

    def fail(
        self,
        key: KeyT,
        *,
        message: str,
        status: str = "failed",
        clear_text: bool = True,
    ) -> None:
        with self._condition:
            state = self._get_or_create_locked(key)
            if clear_text:
                state.text = ""
                state.truncated = False
                state.truncation_notice_sent = False
            state.status = status
            state.terminal = "failed"
            state.expires_at = time.monotonic() + self.terminal_ttl_sec
            self._append_locked(
                key,
                state,
                "failed",
                {
                    "status": status,
                    "message": self._bounded_message(message),
                    "text": state.text[: self.max_event_text_chars],
                    "truncated": state.truncated,
                },
            )

    def fail_if_active(
        self,
        key: KeyT,
        *,
        message: str,
        status: str = "failed",
        clear_text: bool = True,
        expected_stream_id: int | None = None,
    ) -> bool:
        """Atomically fail an existing non-terminal stream, if one remains."""
        with self._condition:
            self._purge_expired_locked()
            state = self._states.get(key)
            if (
                state is None
                or state.terminal is not None
                or (
                    expected_stream_id is not None
                    and state.stream_id != expected_stream_id
                )
            ):
                return False
            if clear_text:
                state.text = ""
                state.truncated = False
                state.truncation_notice_sent = False
            state.status = status
            state.terminal = "failed"
            state.expires_at = time.monotonic() + self.terminal_ttl_sec
            self._append_locked(
                key,
                state,
                "failed",
                {
                    "status": status,
                    "message": self._bounded_message(message),
                    "text": state.text[: self.max_event_text_chars],
                    "truncated": state.truncated,
                },
            )
            return True

    def snapshot(self, key: KeyT) -> dict[str, Any] | None:
        with self._condition:
            self._purge_expired_locked()
            state = self._states.get(key)
            if state is None:
                return None
            self._states.move_to_end(key)
            return self._snapshot_locked(state)

    def _poll_after_locked(
        self,
        key: KeyT,
        sequence: int,
        stream_id: int | None,
    ) -> StreamPoll:
        self._purge_expired_locked()
        state = self._states.get(key)
        if state is None:
            return StreamPoll("missing")
        if stream_id is not None and state.stream_id != stream_id:
            return StreamPoll("missing")
        self._states.move_to_end(key)

        if sequence > state.sequence:
            return StreamPoll("snapshot", snapshot=self._snapshot_locked(state))
        if state.events:
            oldest = state.events[0].sequence
            if sequence < oldest - 1:
                return StreamPoll("snapshot", snapshot=self._snapshot_locked(state))
            events = tuple(event for event in state.events if event.sequence > sequence)
            if events:
                return StreamPoll("events", events=events)
        elif sequence < state.sequence:
            return StreamPoll("snapshot", snapshot=self._snapshot_locked(state))
        return StreamPoll("timeout")

    def poll_after(
        self,
        key: KeyT,
        sequence: int,
        *,
        stream_id: int | None = None,
    ) -> StreamPoll:
        """Read without waiting, explicitly signaling gaps and eviction."""
        with self._condition:
            return self._poll_after_locked(key, sequence, stream_id)

    def events_after(self, key: KeyT, sequence: int) -> list[StreamEvent]:
        with self._condition:
            self._purge_expired_locked()
            state = self._states.get(key)
            if state is None:
                return []
            self._states.move_to_end(key)
            return [event for event in state.events if event.sequence > sequence]

    def wait_for_events(
        self,
        key: KeyT,
        sequence: int,
        *,
        timeout_sec: float,
        stream_id: int | None = None,
    ) -> StreamPoll:
        with self._condition:
            result = self._poll_after_locked(key, sequence, stream_id)
            if result.kind != "timeout":
                return result
            self._condition.wait(timeout=max(0.0, timeout_sec))
            return self._poll_after_locked(key, sequence, stream_id)

    def keys(self) -> Iterable[KeyT]:
        """Small inspection hook used by tests and operational diagnostics."""
        with self._condition:
            self._purge_expired_locked()
            return tuple(self._states.keys())


def encode_sse(event: str, payload: dict[str, Any], *, sequence: int | None = None) -> str:
    """Encode a structured broker event without exposing untrusted raw lines."""
    lines: list[str] = []
    if sequence is not None:
        lines.append(f"id: {sequence}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def build_sse_streaming_response(
    broker: BaseStreamBroker[Any],
    key: Any,
    *,
    initial_snapshot: dict[str, Any] | None = None,
    poll_interval_sec: float = 0.2,
    heartbeat_sec: float = 15.0,
    headers: Mapping[str, str] | None = None,
) -> StreamingResponse:
    """Build a unified SSE StreamingResponse backed by a BaseStreamBroker.

    The first frame is always a complete ``snapshot``, followed by incremental
    events, automatic snapshot resyncs on ring-buffer overflow, periodic
    ``: keep-alive`` heartbeats, and clean termination on terminal events or
    eviction.
    """
    snapshot = dict(initial_snapshot) if initial_snapshot is not None else broker.snapshot(key)
    if snapshot is None:
        snapshot = {
            "stream_id": 0,
            "sequence": 0,
            "status": None,
            "text": "",
            "terminal": None,
            "truncated": False,
        }

    response_headers = dict(DEFAULT_SSE_HEADERS)
    if headers:
        response_headers.update(headers)

    async def event_stream() -> AsyncIterator[str]:
        sequence = int(snapshot.get("sequence") or 0)
        stream_id = int(snapshot.get("stream_id") or 0)
        next_heartbeat_at = time.monotonic() + heartbeat_sec
        yield encode_sse("snapshot", dict(snapshot), sequence=sequence)

        if snapshot.get("terminal") in _TERMINAL_EVENTS:
            return

        while True:
            result = broker.poll_after(key, sequence, stream_id=stream_id)
            if result.kind == "missing":
                return
            if result.kind == "timeout":
                now = time.monotonic()
                if now >= next_heartbeat_at:
                    yield ": keep-alive\n\n"
                    next_heartbeat_at = now + heartbeat_sec
                await asyncio.sleep(poll_interval_sec)
                continue
            if result.kind == "snapshot":
                current = result.snapshot
                if current is None:
                    return
                sequence = int(current.get("sequence") or sequence)
                stream_id = int(current.get("stream_id") or stream_id)
                yield encode_sse("snapshot", dict(current), sequence=sequence)
                if current.get("terminal") in _TERMINAL_EVENTS:
                    return
                continue

            for item in result.events:
                sequence = int(item.sequence)
                yield encode_sse(item.event, item.payload, sequence=sequence)
                if item.event in _TERMINAL_EVENTS:
                    return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=response_headers,
    )


__all__ = [
    "BaseStreamBroker",
    "DEFAULT_SSE_HEADERS",
    "StreamEvent",
    "StreamPoll",
    "_PREVIEW_TRUNCATED_MESSAGE",
    "_TERMINAL_EVENTS",
    "build_sse_streaming_response",
    "encode_sse",
]
