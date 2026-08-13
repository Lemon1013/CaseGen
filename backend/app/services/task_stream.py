"""In-process event broker for live task-generation previews.

This deliberately keeps token updates out of SQLite.  It is suitable for the
single-process deployment used by CaseGen today; running multiple Uvicorn
workers would require a shared broker (for example Redis) so a subscriber and
its background job see the same events.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal


_TERMINAL_EVENTS = frozenset({"completed", "failed"})
_PREVIEW_TRUNCATED_MESSAGE = (
    "实时预览已达到内存上限，后续内容暂不展示；生成完成后会加载完整草稿。"
)


@dataclass(frozen=True)
class TaskStreamEvent:
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
class TaskStreamPoll:
    """Result of reading a task stream after a subscriber sequence."""

    kind: Literal["events", "snapshot", "missing", "timeout"]
    events: tuple[TaskStreamEvent, ...] = ()
    snapshot: dict[str, Any] | None = None


@dataclass
class _TaskStreamState:
    stream_id: int = 0
    sequence: int = 0
    text: str = ""
    status: str | None = None
    terminal: str | None = None
    updated_at: float = field(default_factory=time.monotonic)
    expires_at: float | None = None
    events: deque[TaskStreamEvent] = field(default_factory=deque)
    event_chars: int = 0
    truncated: bool = False
    truncation_notice_sent: bool = False


class TaskStreamBroker:
    """Thread-safe, bounded task stream state with late-subscriber snapshots."""

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
        self._states: OrderedDict[int, _TaskStreamState] = OrderedDict()
        self._condition = threading.Condition(threading.RLock())
        self._next_stream_id = 1

    def _purge_expired_locked(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        expired = [
            task_id
            for task_id, state in self._states.items()
            if state.expires_at is not None and state.expires_at <= current
        ]
        for task_id in expired:
            self._states.pop(task_id, None)
        if expired:
            self._condition.notify_all()

    def _get_or_create_locked(self, task_id: int) -> _TaskStreamState:
        self._purge_expired_locked()
        state = self._states.get(task_id)
        if state is not None:
            self._states.move_to_end(task_id)
            return state
        while len(self._states) >= self.max_tasks:
            self._states.popitem(last=False)
            # Wake a subscriber whose state was evicted so it can close rather
            # than heartbeat forever with a sequence that no longer exists.
            self._condition.notify_all()
        state = _TaskStreamState(stream_id=self._next_stream_id)
        self._next_stream_id += 1
        self._states[task_id] = state
        return state

    def discard(self, task_id: int) -> None:
        """Discard retained state for a newly-created database task.

        SQLite may reuse an integer task id after deletion. Removing retained
        state prevents old preview text crossing that lifecycle boundary and
        wakes old subscribers. Draft-only tasks consume no broker capacity;
        ``start`` creates state only when generation really begins.
        """
        with self._condition:
            self._purge_expired_locked()
            self._states.pop(task_id, None)
            self._condition.notify_all()

    def _bounded_message(self, message: str) -> str:
        return str(message or "")[: self.max_message_chars]

    def _snapshot_locked(self, state: _TaskStreamState) -> dict[str, Any]:
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
        task_id: int,
        state: _TaskStreamState,
    ) -> None:
        if state.truncation_notice_sent:
            return
        state.truncation_notice_sent = True
        self._append_locked(
            task_id,
            state,
            "notice",
            {"message": _PREVIEW_TRUNCATED_MESSAGE, "truncated": True},
        )

    def _append_locked(
        self,
        task_id: int,
        state: _TaskStreamState,
        event: str,
        payload: dict[str, Any],
    ) -> TaskStreamEvent:
        state.sequence += 1
        state.updated_at = time.monotonic()
        item = TaskStreamEvent(state.sequence, event, payload)
        state.events.append(item)
        state.event_chars += self._event_payload_chars(payload)
        while (
            len(state.events) > self.max_events_per_task
            or state.event_chars > self.max_event_history_chars
        ):
            removed = state.events.popleft()
            state.event_chars -= self._event_payload_chars(removed.payload)
        self._states.move_to_end(task_id)
        self._condition.notify_all()
        return item

    @staticmethod
    def _event_payload_chars(payload: dict[str, Any]) -> int:
        """Conservative character accounting for bounded history retention."""
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def start(self, task_id: int, *, status: str, message: str = "") -> None:
        """Start a new generation run and discard any prior live preview."""
        with self._condition:
            state = self._get_or_create_locked(task_id)
            state.text = ""
            state.status = status
            state.terminal = None
            state.expires_at = None
            state.truncated = False
            state.truncation_notice_sent = False
            self._append_locked(
                task_id,
                state,
                "reset",
                {
                    "status": status,
                    "message": self._bounded_message(message),
                    "text": "",
                },
            )
            self._append_locked(
                task_id,
                state,
                "status",
                {"status": status, "message": self._bounded_message(message)},
            )

    def ensure(self, task_id: int, *, status: str, terminal: str | None = None) -> None:
        """Create a snapshot state for a task that predates the live broker."""
        with self._condition:
            if task_id in self._states:
                self._states.move_to_end(task_id)
                return
            state = self._get_or_create_locked(task_id)
            state.status = status
            if terminal in _TERMINAL_EVENTS:
                state.terminal = terminal
                state.expires_at = time.monotonic() + self.terminal_ttl_sec

    def status(self, task_id: int, *, status: str, message: str = "") -> None:
        with self._condition:
            state = self._get_or_create_locked(task_id)
            state.status = status
            self._append_locked(
                task_id,
                state,
                "status",
                {"status": status, "message": self._bounded_message(message)},
            )

    def delta(self, task_id: int, text: str) -> None:
        if not text:
            return
        with self._condition:
            state = self._get_or_create_locked(task_id)
            if state.terminal is not None:
                return
            if state.truncated:
                return

            remaining = self.max_preview_chars - len(state.text)
            accepted = text[:remaining]
            for offset in range(0, len(accepted), self.max_event_text_chars):
                chunk = accepted[offset : offset + self.max_event_text_chars]
                state.text += chunk
                self._append_locked(task_id, state, "delta", {"delta": chunk})

            if len(text) > len(accepted):
                state.truncated = True
                self._publish_truncation_notice_locked(task_id, state)

    def reset(self, task_id: int, *, message: str = "", status: str | None = None) -> None:
        with self._condition:
            state = self._get_or_create_locked(task_id)
            state.text = ""
            state.truncated = False
            state.truncation_notice_sent = False
            if status is not None:
                state.status = status
            self._append_locked(
                task_id,
                state,
                "reset",
                {
                    "status": state.status,
                    "message": self._bounded_message(message),
                    "text": "",
                },
            )

    def retry(self, task_id: int, *, message: str, attempt: int | None = None) -> None:
        payload: dict[str, Any] = {"message": self._bounded_message(message)}
        if attempt is not None:
            payload["attempt"] = attempt
        with self._condition:
            state = self._get_or_create_locked(task_id)
            self._append_locked(task_id, state, "retry", payload)

    def notice(self, task_id: int, *, message: str) -> None:
        with self._condition:
            state = self._get_or_create_locked(task_id)
            self._append_locked(
                task_id,
                state,
                "notice",
                {"message": self._bounded_message(message)},
            )

    def complete(
        self,
        task_id: int,
        *,
        text: str,
        status: str = "generated",
        message: str = "生成完成",
    ) -> None:
        with self._condition:
            state = self._get_or_create_locked(task_id)
            # The returned response is authoritative, but only the temporary
            # preview is retained here. The pipeline has already persisted the
            # complete draft before calling this method.
            state.text = text[: self.max_preview_chars]
            state.truncated = len(text) > len(state.text)
            if state.truncated:
                self._publish_truncation_notice_locked(task_id, state)
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
            # Avoid replacing a larger client-side preview with an arbitrarily
            # clipped terminal value. Small previews keep the legacy contract.
            if len(state.text) <= self.max_event_text_chars:
                terminal_payload["text"] = state.text
            self._append_locked(
                task_id,
                state,
                "completed",
                terminal_payload,
            )

    def fail(
        self,
        task_id: int,
        *,
        message: str,
        status: str = "failed",
        clear_text: bool = True,
    ) -> None:
        with self._condition:
            state = self._get_or_create_locked(task_id)
            if clear_text:
                state.text = ""
                state.truncated = False
                state.truncation_notice_sent = False
            state.status = status
            state.terminal = "failed"
            state.expires_at = time.monotonic() + self.terminal_ttl_sec
            self._append_locked(
                task_id,
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
        task_id: int,
        *,
        message: str,
        status: str = "failed",
        clear_text: bool = True,
        expected_stream_id: int | None = None,
    ) -> bool:
        """Atomically fail an existing non-terminal stream, if one remains."""
        with self._condition:
            self._purge_expired_locked()
            state = self._states.get(task_id)
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
                task_id,
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

    def snapshot(self, task_id: int) -> dict[str, Any] | None:
        with self._condition:
            self._purge_expired_locked()
            state = self._states.get(task_id)
            if state is None:
                return None
            self._states.move_to_end(task_id)
            return self._snapshot_locked(state)

    def _poll_after_locked(
        self,
        task_id: int,
        sequence: int,
        stream_id: int | None,
    ) -> TaskStreamPoll:
        self._purge_expired_locked()
        state = self._states.get(task_id)
        if state is None:
            return TaskStreamPoll("missing")
        if stream_id is not None and state.stream_id != stream_id:
            return TaskStreamPoll("missing")
        self._states.move_to_end(task_id)

        if sequence > state.sequence:
            return TaskStreamPoll("snapshot", snapshot=self._snapshot_locked(state))
        if state.events:
            oldest = state.events[0].sequence
            if sequence < oldest - 1:
                return TaskStreamPoll("snapshot", snapshot=self._snapshot_locked(state))
            events = tuple(event for event in state.events if event.sequence > sequence)
            if events:
                return TaskStreamPoll("events", events=events)
        elif sequence < state.sequence:
            return TaskStreamPoll("snapshot", snapshot=self._snapshot_locked(state))
        return TaskStreamPoll("timeout")

    def poll_after(
        self,
        task_id: int,
        sequence: int,
        *,
        stream_id: int | None = None,
    ) -> TaskStreamPoll:
        """Read without waiting, explicitly signaling gaps and eviction."""
        with self._condition:
            return self._poll_after_locked(task_id, sequence, stream_id)

    def events_after(self, task_id: int, sequence: int) -> list[TaskStreamEvent]:
        with self._condition:
            self._purge_expired_locked()
            state = self._states.get(task_id)
            if state is None:
                return []
            self._states.move_to_end(task_id)
            return [event for event in state.events if event.sequence > sequence]

    def wait_for_events(
        self,
        task_id: int,
        sequence: int,
        *,
        timeout_sec: float,
        stream_id: int | None = None,
    ) -> TaskStreamPoll:
        with self._condition:
            result = self._poll_after_locked(task_id, sequence, stream_id)
            if result.kind != "timeout":
                return result
            self._condition.wait(timeout=max(0.0, timeout_sec))
            return self._poll_after_locked(task_id, sequence, stream_id)

    def task_ids(self) -> Iterable[int]:
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


task_stream = TaskStreamBroker()
