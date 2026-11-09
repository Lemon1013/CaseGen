"""Per-conversation live stream broker for the agent chat (line A).

This is the string-keyed sibling of ``app.services.task_stream``.  The
capacity parameters, event names and snapshot/poll semantics are inherited
unchanged so the browser contract stays identical; only the key type differs
(agent conversation ids are uuid strings, generation task ids are integers).

Single-process constraint: like ``task_stream``, this broker lives in memory.
CaseGen runs one uvicorn process, so a worker thread can publish and the SSE
endpoint in the same process can read.  A multi-worker deployment would need
a shared broker (Redis); that is explicitly out of scope for P1.

Wire payload keys (pinned with the frontend, see spec 3.2):

- ``delta`` frames carry ``{"delta": "<increment>"}``
- ``snapshot`` / ``completed`` frames carry the whole assistant body in
  ``text`` (``completed`` only when it fits ``max_event_text_chars``)
- ``failed`` frames carry ``message`` (presentable Chinese) plus an extra
  ``category`` key from the gateway error taxonomy

The ``category`` key is an agent-plane extension.  `TaskStreamBroker.fail`
builds a fixed four-key payload, so the category is recorded in a small
side table here and injected into both the `failed` event payload and any
`snapshot` of a failed stream.  Enriching the snapshot matters: a browser
that reloads mid-cancel never sees the live `failed` frame, and without a
category on the snapshot it cannot tell a user-initiated cancel from a real
error.  The side table is bounded and cleared on `start` / `complete` /
`discard` so it cannot outlive the stream it describes.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from typing import Any

from app.services.task_stream import (
    TaskStreamBroker,
    TaskStreamPoll,
    encode_sse,
)

__all__ = ["AgentStreamBroker", "agent_stream", "encode_sse"]

# Categories are tiny fixed strings, but bound the table anyway so a long-lived
# process cannot accumulate one entry per conversation ever created.
_MAX_CATEGORY_ENTRIES = 256
# Same bound for the reasoning side table: one entry per live/trailing stream.
_MAX_REASONING_ENTRIES = 256


class AgentStreamBroker(TaskStreamBroker):
    """Keyed by agent conversation id (str) instead of task id (int).

    State, eviction, truncation and polling behaviour are inherited.  The
    overrides below exist only to attach the agent failure ``category`` to the
    wire payloads, and to give the agent plane its own singleton so conversation
    ids can never collide with task ids.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Guarded by the inherited condition (an RLock), which every override
        # below already holds or re-enters.
        self._categories: OrderedDict[str, str] = OrderedDict()
        # Accumulated thinking-process text per conversation.  It is a side
        # table (not part of _TaskStreamState) so the inherited event/snapshot
        # machinery stays untouched; the snapshot override folds it back in.
        self._reasoning: OrderedDict[str, str] = OrderedDict()

    # -- category side table ------------------------------------------------

    def _set_category_locked(self, conversation_id: str, category: str) -> None:
        key = str(conversation_id)
        value = str(category or "").strip()
        if not value:
            self._categories.pop(key, None)
            return
        self._categories[key] = value
        self._categories.move_to_end(key)
        while len(self._categories) > _MAX_CATEGORY_ENTRIES:
            self._categories.popitem(last=False)

    def _category_locked(self, conversation_id: str) -> str:
        return self._categories.get(str(conversation_id), "")

    # -- reasoning side table ----------------------------------------------

    def _set_reasoning_locked(
        self, conversation_id: str, text: str, *, append: bool = True
    ) -> None:
        key = str(conversation_id)
        if not append:
            self._reasoning.pop(key, None)
            return
        if not text:
            return
        value = self._reasoning.get(key, "") + str(text)
        # Keep only the most recent max_preview_chars characters; older
        # thinking is no longer useful on reconnect and must stay bounded.
        if len(value) > self.max_preview_chars:
            value = value[-self.max_preview_chars :]
        self._reasoning[key] = value
        self._reasoning.move_to_end(key)
        while len(self._reasoning) > _MAX_REASONING_ENTRIES:
            self._reasoning.popitem(last=False)

    def _enrich_snapshot_locked(
        self, conversation_id: str, snapshot: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Return a copy carrying the reasoning text and any failure category."""

        if not snapshot:
            return snapshot
        enriched = dict(snapshot)
        reasoning = self._reasoning.get(str(conversation_id))
        if reasoning:
            enriched["reasoning"] = reasoning
        if enriched.get("terminal") == "failed":
            category = self._category_locked(conversation_id)
            if category and "category" not in enriched:
                enriched["category"] = category
        return enriched

    # -- inherited hooks that know the key ----------------------------------

    def _append_locked(
        self,
        task_id: Any,
        state: Any,
        event: str,
        payload: dict[str, Any],
    ) -> Any:
        if event == "failed":
            category = self._category_locked(task_id)
            if category and "category" not in payload:
                payload = {**payload, "category": category}
        return super()._append_locked(task_id, state, event, payload)

    def _poll_after_locked(
        self,
        task_id: Any,
        sequence: int,
        stream_id: int | None,
    ) -> TaskStreamPoll:
        result = super()._poll_after_locked(task_id, sequence, stream_id)
        if result.kind == "snapshot":
            return replace(
                result, snapshot=self._enrich_snapshot_locked(task_id, result.snapshot)
            )
        return result

    # -- public API ---------------------------------------------------------

    def start(self, conversation_id: str, *, status: str, message: str = "") -> None:
        with self._condition:
            self._set_category_locked(conversation_id, "")
            self._set_reasoning_locked(conversation_id, "", append=False)
            super().start(conversation_id, status=status, message=message)

    def complete(
        self,
        conversation_id: str,
        *,
        text: str,
        status: str = "generated",
        message: str = "生成完成",
    ) -> None:
        with self._condition:
            self._set_category_locked(conversation_id, "")
            super().complete(conversation_id, text=text, status=status, message=message)

    def fail(
        self,
        conversation_id: str,
        *,
        message: str,
        status: str = "failed",
        clear_text: bool = True,
        category: str = "",
    ) -> None:
        """Publish the terminal failed frame, tagged with an error category."""

        with self._condition:
            # Recorded before the inherited call so _append_locked can inject it
            # into the very payload this failure publishes.
            self._set_category_locked(conversation_id, category)
            super().fail(
                conversation_id,
                message=message,
                status=status,
                clear_text=clear_text,
            )

    def fail_if_active(
        self,
        conversation_id: str,
        *,
        message: str,
        status: str = "failed",
        clear_text: bool = True,
        expected_stream_id: int | None = None,
        category: str = "",
    ) -> bool:
        with self._condition:
            self._set_category_locked(conversation_id, category)
            published = super().fail_if_active(
                conversation_id,
                message=message,
                status=status,
                clear_text=clear_text,
                expected_stream_id=expected_stream_id,
            )
            if not published:
                # Nothing went out, so the tag must not linger either.
                self._set_category_locked(conversation_id, "")
            return published

    def reasoning(
        self,
        conversation_id: str,
        *,
        text: str = "",
        reset_answer: bool = False,
    ) -> None:
        """Publish a live thinking chunk; reset_answer clears temp answer text.

        Only active (non-terminal) streams accept reasoning frames.  The text
        is accumulated in the bounded side table so a reconnecting browser can
        recover the whole thinking process from the snapshot, while terminal
        streams keep the value for the snapshot TTL.
        """

        key = str(conversation_id)
        with self._condition:
            state = self._states.get(key)
            if state is None or state.terminal is not None:
                return
            if text:
                self._set_reasoning_locked(key, text, append=True)
            payload: dict[str, Any] = {"delta": text}
            if reset_answer:
                payload["reset_answer"] = True
            self._states.move_to_end(key)
            self._append_locked(key, state, "reasoning", payload)

    def snapshot(self, conversation_id: str) -> dict[str, Any] | None:
        with self._condition:
            return self._enrich_snapshot_locked(
                conversation_id, super().snapshot(conversation_id)
            )

    def discard(self, conversation_id: str) -> None:
        with self._condition:
            self._set_category_locked(conversation_id, "")
            self._set_reasoning_locked(conversation_id, "", append=False)
            super().discard(conversation_id)

    def terminal_payload(self, conversation_id: str) -> dict[str, Any] | None:
        """Small inspection hook used by tests and operational diagnostics."""

        return self.snapshot(conversation_id)


# Conversation ids are uuid hex strings; the inherited implementation only
# uses them as dict keys, so no further change is required.
agent_stream = AgentStreamBroker()
