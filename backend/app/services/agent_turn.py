"""Background runner for one agent chat turn (line A).

Design rules that shape this module:

- Never hold a SQLite session across the DSH turn.  Every DB touch is a short
  "with Session(get_engine())" block that commits and closes immediately
  (same discipline as the per-stage commits in task_pipeline).
- Never auto-retry a prompt.  Re-sending POST /sessions/:id/prompt-stream
  re-runs the whole agent turn, so a transport failure surfaces exactly once.
- Never set a module-level chat hook.  The line-B pipeline hooks change the
  execution path of 60+ existing test files; this module does not touch them.
- Never forward tool_call / tool_result / usage frames.  The gateway already
  drops them; this runner publishes delta / reasoning / notice / status /
  terminal frames to the in-memory broker.  Reasoning is the user-approved
  thinking process and is also persisted on the assistant message.
- Delta frames carry only the final answer.  Assistant text emitted before a
  tool boundary is commentary, so it is folded into the reasoning stream
  instead of being published as answer text; the buffered answer is discarded
  at each boundary and only flushed when the loop ends normally.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlmodel import Session, col, select

from app import config
from app.db import get_engine
from app.models.entities import AgentConversation, AgentMessage, Project, WikiSpace
from app.services import agent_gateway
from app.services.agent_gateway import (
    AgentGatewayError,
    DshTurnStats,
    safe_agent_error,
)
from app.services.agent_skills import skill_prompt_prefix
from app.services.agent_stream import agent_stream
from app.services.projects import project_space_ids
from app.services.wiki_spaces import ensure_space_dirs, space_root

logger = logging.getLogger(__name__)

__all__ = [
    "STATUS_CONNECTING",
    "TurnHandle",
    "start_turn",
    "run_turn",
    "request_cancel",
    "is_cancel_requested",
    "active_turn_ids",
    "failed_category",
]

_BUSY_STATUS = "busy"
_IDLE_STATUS = "active"

# Fixed, user-presentable copy (the frontend shows the message verbatim).
_STATUS_CONNECTING = "正在连接 Agent 会话…"
_STATUS_STREAMING = "Agent 正在处理…"
_STATUS_CONNECTED = "已连接 Agent 会话"
_MESSAGE_COMPLETED = "本轮回答已完成"
_MESSAGE_CANCELLED = "已取消本轮对话"
_MESSAGE_STALL = "Agent 长时间无响应，本轮已终止。"

# Public alias: the API layer starts the live preview with the same copy the
# worker publishes afterwards, so the first frame and the following ones agree.
STATUS_CONNECTING = _STATUS_CONNECTING

# Delta frames are batched before they reach the broker so a fast model cannot
# exhaust max_events_per_task with one event per token.
_FLUSH_INTERVAL_SEC = 0.15
_FLUSH_CHARS = 2_000
# Cancel flags are re-read from SQLite at most this often.
_CANCEL_POLL_SEC = 0.5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TurnHandle:
    """Bookkeeping for one in-flight turn inside this process."""

    __slots__ = (
        "conversation_id",
        "stream_id",
        "thread",
        "cancel_requested",
        "stall_dead",
        "_lock",
        "_last_activity",
        "_last_poll",
        "_durable_cancel",
    )

    def __init__(self, conversation_id: str, stream_id: int) -> None:
        self.conversation_id = str(conversation_id)
        self.stream_id = int(stream_id)
        self.thread: Optional[threading.Thread] = None
        self.cancel_requested = False
        self.stall_dead = False
        self._lock = threading.Lock()
        self._last_activity = time.monotonic()
        self._last_poll = 0.0
        self._durable_cancel = False

    def touch(self) -> None:
        with self._lock:
            self._last_activity = time.monotonic()

    def seconds_since_activity(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_activity

    def should_stop(self) -> bool:
        """Cooperative cancel predicate polled by the gateway between frames."""

        if self.cancel_requested or self.stall_dead:
            return True
        # The durable flag lives in SQLite, so it is re-read at most twice a
        # second instead of once per DSH frame (frames can arrive per token).
        now = time.monotonic()
        if now - self._last_poll >= _CANCEL_POLL_SEC:
            with self._lock:
                self._last_poll = now
            self._durable_cancel = is_cancel_requested(self.conversation_id)
        return self._durable_cancel


class _TurnRegistry:
    """Process-local map of running turns, mirroring TaskLockRegistry hygiene."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._handles: dict[str, TurnHandle] = {}
        # Last failed-frame category per conversation, so the SSE endpoint can
        # enrich the terminal payload without holding extra broker state.
        self._categories: dict[str, str] = {}

    def register(self, handle: TurnHandle) -> None:
        with self._guard:
            self._handles[handle.conversation_id] = handle

    def pop(self, conversation_id: str) -> Optional[TurnHandle]:
        with self._guard:
            self._categories.pop(str(conversation_id), None)
            return self._handles.pop(str(conversation_id), None)

    def get(self, conversation_id: str) -> Optional[TurnHandle]:
        with self._guard:
            return self._handles.get(str(conversation_id))

    def ids(self) -> list[str]:
        with self._guard:
            return sorted(self._handles)

    def set_category(self, conversation_id: str, category: str) -> None:
        with self._guard:
            self._categories[str(conversation_id)] = str(category)

    def clear_category(self, conversation_id: str) -> None:
        with self._guard:
            self._categories.pop(str(conversation_id), None)

    def category(self, conversation_id: str) -> str:
        with self._guard:
            return self._categories.get(str(conversation_id), "")


_registry = _TurnRegistry()


def active_turn_ids() -> list[str]:
    """Inspection hook used by tests and operational diagnostics."""

    return _registry.ids()


def failed_category(conversation_id: str) -> str:
    """Category of the last failed frame, empty when unknown or not failed."""

    return _registry.category(conversation_id)


def is_cancel_requested(conversation_id: str) -> bool:
    """Read the durable cancel flag with a short-lived session."""

    try:
        with Session(get_engine()) as session:
            conversation = session.get(AgentConversation, str(conversation_id))
            if conversation is None:
                # A deleted conversation must stop the turn.
                return True
            return bool(conversation.cancel_requested)
    except Exception:  # pragma: no cover - defensive; never leak internals
        logger.warning("读取 Agent 取消标志失败，按未取消处理。")
        return False


def request_cancel(conversation_id: str) -> bool:
    """Flip the durable cancel flag.  Returns False when the turn is not busy."""

    with agent_gateway.conversation_locks.hold(str(conversation_id)):
        with Session(get_engine()) as session:
            conversation = session.get(AgentConversation, str(conversation_id))
            if conversation is None:
                return False
            if conversation.status != _BUSY_STATUS:
                return False
            conversation.cancel_requested = True
            conversation.version = int(conversation.version or 1) + 1
            conversation.updated_at = _utcnow()
            session.add(conversation)
            session.commit()
    handle = _registry.get(conversation_id)
    if handle is not None:
        handle.cancel_requested = True
    return True


# ---------------------------------------------------------------------------
# Durable state helpers (each opens and closes its own session)
# ---------------------------------------------------------------------------


def _load_turn_inputs(conversation_id: str) -> Optional[dict[str, Any]]:
    with Session(get_engine()) as session:
        conversation = session.get(AgentConversation, str(conversation_id))
        if conversation is None:
            return None
        last_user = session.exec(
            select(AgentMessage)
            .where(AgentMessage.conversation_id == str(conversation_id))
            .where(AgentMessage.role == "user")
            .order_by(col(AgentMessage.created_at).desc(), col(AgentMessage.id).desc())
        ).first()
        return {
            "title": conversation.title or "",
            "model": conversation.model,
            "skill_name": conversation.skill_name,
            "dsh_session_id": conversation.dsh_session_id,
            "dsh_workspace_id": conversation.dsh_workspace_id,
            "project_id": conversation.project_id or "",
            "prompt": (last_user.content if last_user is not None else "") or "",
        }


def _persist_session_ids(
    conversation_id: str, session_id: str, workspace_id: str, workspace_dir: str
) -> None:
    with Session(get_engine()) as session:
        conversation = session.get(AgentConversation, str(conversation_id))
        if conversation is None:
            return
        conversation.dsh_session_id = str(session_id)
        conversation.dsh_workspace_id = str(workspace_id or "")
        conversation.workspace_dir = str(workspace_dir or "")
        session.add(conversation)
        session.commit()


def _finish_turn(
    conversation_id: str,
    *,
    assistant_text: str,
    reasoning: str = "",
) -> None:
    """Persist the assistant answer and/or thinking process, then go idle.

    The final answer (assistant_text) and the thinking process (reasoning) are
    stored in separate columns.  A cancelled turn keeps whatever the agent
    already produced so the user can still read it (and its thinking) after
    refreshing; the terminal SSE frame carries the same answer text.
    """

    with Session(get_engine()) as session:
        conversation = session.get(AgentConversation, str(conversation_id))
        if conversation is None:
            return
        text = (assistant_text or "").strip()
        thought = (reasoning or "").strip()
        if text or thought:
            session.add(
                AgentMessage(
                    conversation_id=str(conversation_id),
                    role="assistant",
                    content=text,
                    reasoning=thought,
                )
            )
        conversation.status = _IDLE_STATUS
        conversation.cancel_requested = False
        conversation.version = int(conversation.version or 1) + 1
        conversation.updated_at = _utcnow()
        session.add(conversation)
        session.commit()


def _release_conversation(conversation_id: str) -> None:
    """Return a conversation to idle without persisting an assistant body."""

    with Session(get_engine()) as session:
        conversation = session.get(AgentConversation, str(conversation_id))
        if conversation is None:
            return
        conversation.status = _IDLE_STATUS
        conversation.cancel_requested = False
        conversation.version = int(conversation.version or 1) + 1
        conversation.updated_at = _utcnow()
        session.add(conversation)
        session.commit()


def _knowledge_space_dirs(project_id: str) -> list[str]:
    """Return absolute Wiki space dirs visible to this project (one short DB session)."""
    try:
        pid = int(str(project_id).strip())
    except (TypeError, ValueError):
        return []
    try:
        with Session(get_engine()) as session:
            try:
                space_ids = project_space_ids(session, pid)
            except Exception:
                # Project deleted / inactive must not break the chat turn.
                return []
            project = session.get(Project, pid)
            preferred = (
                int(project.default_wiki_space_id)
                if project is not None and project.default_wiki_space_id is not None
                else None
            )
            ordered = ([preferred] if preferred in space_ids else []) + [
                sid for sid in space_ids if sid != preferred
            ]
            dirs: list[str] = []
            for sid in ordered:
                space = session.get(WikiSpace, int(sid))
                if space is None:
                    continue
                try:
                    root = ensure_space_dirs(space)
                except (OSError, ValueError):
                    root = space_root(space)
                if root.is_dir() and str(root) not in dirs:
                    dirs.append(str(root))
            return dirs
    except Exception:
        # Knowledge lookup is best-effort: never let it fail a chat turn.
        return []


def _knowledge_prompt_block(dirs: list[str]) -> str:
    if not dirs:
        return ""
    lines = "\n".join(f"- {p}" for p in dirs)
    return (
        "[知识空间]\n"
        "本项目的知识库以 Markdown 文件存放在以下目录（只读）：\n"
        f"{lines}\n"
        "需要背景资料时，请自行用文件检索工具（glob / grep / read）在这些目录内查阅相关文档，不要凭空编造。\n"
        "严格限制：只允许在上述列出的目录内检索和阅读，不要访问其他知识空间目录（包括 data/wiki/spaces/ 下的其它空间），不要读取这些目录之外的任何文件，也不要把检索范围扩大到整个项目或磁盘。\n\n"
    )


def _build_prompt(
    user_text: str, skill_name: Optional[str], knowledge_dirs: Optional[list[str]] = None
) -> str:
    return (
        skill_prompt_prefix(skill_name or "")
        + _knowledge_prompt_block(knowledge_dirs or [])
        + (user_text or "")
    )


# ---------------------------------------------------------------------------
# Stall watchdog
# ---------------------------------------------------------------------------


def _watchdog(handle: TurnHandle, stop_event: threading.Event) -> None:
    """Fail a turn that receives no DSH frame at all for too long.

    DSH's own watchdog is a stall timer, not a wall clock, and it emits a
    ": ping" comment every 15s while a model loads.  The gateway surfaces
    pings as liveness events and the runner touches the handle on every frame,
    so this only fires when the socket itself goes silent.
    """

    budget = max(10.0, float(getattr(config, "AGENT_STALL_TIMEOUT_SEC", 300)))
    while not stop_event.wait(min(1.0, budget)):
        if handle.seconds_since_activity() >= budget:
            handle.stall_dead = True
            logger.warning("Agent 轮次长时间无响应，触发停止。")
            return


# ---------------------------------------------------------------------------
# Turn execution
# ---------------------------------------------------------------------------


def run_turn(
    conversation_id: str,
    *,
    transport: Optional[httpx.BaseTransport] = None,
    handle: Optional[TurnHandle] = None,
) -> dict[str, Any]:
    """Execute one agent turn synchronously and return a summary dict.

    The summary is for tests and logs only; it never contains reasoning, tool
    arguments or tool results.  The browser sees the broker events.
    """

    conversation_id = str(conversation_id)
    active_handle = handle
    if active_handle is None:
        snapshot = agent_stream.snapshot(conversation_id)
        stream_id = int((snapshot or {}).get("stream_id") or 0)
        active_handle = TurnHandle(conversation_id, stream_id)
        _registry.register(active_handle)

    stats = DshTurnStats()
    collected: list[str] = []
    buffer: list[str] = []
    buffer_lock = threading.Lock()
    last_flush = time.monotonic()
    reasoning_parts: list[str] = []
    reasoning_buffer: list[str] = []
    reasoning_lock = threading.Lock()
    last_reasoning_flush = time.monotonic()
    stop_event = threading.Event()
    watchdog_thread: Optional[threading.Thread] = None

    def flush_buffer(force: bool = False) -> None:
        nonlocal last_flush
        with buffer_lock:
            if not buffer:
                return
            pending = sum(len(chunk) for chunk in buffer)
            now = time.monotonic()
            if not force and now - last_flush < _FLUSH_INTERVAL_SEC and pending < _FLUSH_CHARS:
                return
            text = "".join(buffer)
            buffer.clear()
            last_flush = now
        agent_stream.delta(conversation_id, text)

    def discard_buffer() -> None:
        # Drop buffered answer text without publishing it.  Text before a tool
        # boundary is commentary, folded into the reasoning stream by the
        # caller, so it must never reach the live answer bubble.
        with buffer_lock:
            buffer.clear()

    def flush_reasoning(force: bool = False) -> None:
        nonlocal last_reasoning_flush
        with reasoning_lock:
            if not reasoning_buffer:
                return
            pending = sum(len(chunk) for chunk in reasoning_buffer)
            now = time.monotonic()
            if (
                not force
                and now - last_reasoning_flush < _FLUSH_INTERVAL_SEC
                and pending < _FLUSH_CHARS
            ):
                return
            text = "".join(reasoning_buffer)
            reasoning_buffer.clear()
            last_reasoning_flush = now
        agent_stream.reasoning(conversation_id, text=text)

    try:
        inputs = _load_turn_inputs(conversation_id)
        if inputs is None:
            _registry.set_category(conversation_id, "not_found")
            agent_stream.fail(
                conversation_id,
                message="对话不存在或已被删除。",
                status=_IDLE_STATUS,
                clear_text=False,
                category="not_found",
            )
            return {"outcome": "failed", "category": "not_found", "text": ""}

        raw_user_text = inputs["prompt"] or ""
        knowledge_dirs = _knowledge_space_dirs(str(inputs.get("project_id") or ""))
        prompt = (
            _build_prompt(raw_user_text, inputs.get("skill_name"), knowledge_dirs)
            if raw_user_text.strip()
            else ""
        )
        if not prompt.strip():
            _registry.set_category(conversation_id, "bad_response")
            agent_stream.fail(
                conversation_id,
                message="消息内容为空，无法发起 Agent 轮次。",
                status=_IDLE_STATUS,
                clear_text=False,
                category="bad_response",
            )
            _release_conversation(conversation_id)
            return {"outcome": "failed", "category": "bad_response", "text": ""}

        agent_stream.status(conversation_id, status=_BUSY_STATUS, message=_STATUS_CONNECTING)

        # The global semaphore bounds concurrent DSH turns.  The per-conversation
        # mutex is deliberately NOT held here: a turn lasts minutes and the API
        # layer already rejects a second message with 409 while status is busy.
        with agent_gateway.agent_semaphore.acquire():
            agent_stream.status(conversation_id, status=_BUSY_STATUS, message=_STATUS_STREAMING)
            dsh_session_id = str(inputs.get("dsh_session_id") or "").strip()
            if not dsh_session_id:
                provisioned = agent_gateway.ensure_session(
                    conversation_id=conversation_id,
                    title=inputs.get("title") or "",
                    model=inputs.get("model"),
                    workspace_id=inputs.get("dsh_workspace_id"),
                    agent_preset=getattr(config, "AGENT_PRESET", "") or None,
                    transport=transport,
                )
                dsh_session_id = provisioned.session_id
                active_handle.touch()
                _persist_session_ids(
                    conversation_id,
                    provisioned.session_id,
                    provisioned.workspace_id,
                    provisioned.cwd,
                )

            stop_event.clear()
            watchdog_thread = threading.Thread(
                target=_watchdog,
                args=(active_handle, stop_event),
                name="agent-stall-" + conversation_id[:8],
                daemon=True,
            )
            watchdog_thread.start()

            connected = False
            terminal_seen = False
            outcome = "completed"
            category = ""
            message = _MESSAGE_COMPLETED
            for event in agent_gateway.stream_prompt(
                dsh_session_id,
                prompt,
                should_stop=active_handle.should_stop,
                stats=stats,
                transport=transport,
            ):
                active_handle.touch()
                kind = event.kind
                if kind == "ping":
                    # Liveness only; never forwarded downstream.
                    continue
                if kind == "connected":
                    connected = True
                    agent_stream.status(
                        conversation_id, status=_BUSY_STATUS, message=_STATUS_CONNECTED
                    )
                    continue
                if kind == "reasoning":
                    text = event.text
                    if text:
                        reasoning_parts.append(text)
                        with reasoning_lock:
                            reasoning_buffer.append(text)
                        flush_reasoning()
                    continue
                if kind == "delta":
                    # DSH sends one delta per assistant message, so buffering
                    # until a boundary or the end loses no visible streaming.
                    # Publishing early would show commentary in the answer
                    # bubble before it is folded into reasoning.
                    collected.append(event.text)
                    with buffer_lock:
                        buffer.append(event.text)
                    continue
                if kind in ("notice", "tool_call"):
                    # Any tool boundary ends a commentary segment: drop the
                    # buffered answer text (it is commentary, never the final
                    # answer), fold it plus any pending reasoning into the
                    # thinking process, then tell the browser to clear its
                    # temporary answer preview.  Only a whitelisted tool
                    # carries human-readable notice text.
                    discard_buffer()
                    flush_reasoning(force=True)
                    commentary = "".join(collected).strip()
                    if commentary:
                        prefix = "\n\n" if reasoning_parts else ""
                        reasoning_parts.append(prefix + commentary)
                        agent_stream.reasoning(
                            conversation_id,
                            text=prefix + commentary,
                            reset_answer=True,
                        )
                        collected.clear()
                    if event.text:
                        agent_stream.notice(conversation_id, message=event.text)
                    continue
                if kind == "completed":
                    terminal_seen = True
                    outcome = "completed"
                    category = ""
                    message = _MESSAGE_COMPLETED
                    break
                if kind == "failed":
                    terminal_seen = True
                    outcome = "failed"
                    category = event.category or "stream_error"
                    message = event.text or agent_gateway._category_message(category)
                    break

            stop_event.set()
            flush_buffer(force=True)
            flush_reasoning(force=True)

            if not terminal_seen:
                # A cooperative stop, a dead socket, or a bare EOF.  The gateway
                # ends the iterator without a terminal event in these cases.
                if active_handle.cancel_requested or is_cancel_requested(conversation_id):
                    outcome = "failed"
                    category = "cancelled"
                    message = _MESSAGE_CANCELLED
                elif active_handle.stall_dead:
                    outcome = "failed"
                    category = "timeout"
                    message = _MESSAGE_STALL
                else:
                    outcome = "failed"
                    category = "stream_error"
                    message = agent_gateway._category_message("stream_error")
            elif category == "cancelled":
                message = message or _MESSAGE_CANCELLED

            body = "".join(collected).strip()
            thought = "".join(reasoning_parts).strip()
            # Persist first, publish after the commit, so the SSE terminal frame
            # always agrees with durable state (the task_pipeline ordering).
            _finish_turn(conversation_id, assistant_text=body, reasoning=thought)
            if outcome == "completed":
                _registry.clear_category(conversation_id)
                agent_stream.complete(
                    conversation_id, text=body, status=_IDLE_STATUS, message=message
                )
            else:
                failure_category = category or "unknown"
                _registry.set_category(conversation_id, failure_category)
                agent_stream.fail(
                    conversation_id,
                    message=message or agent_gateway._category_message(failure_category),
                    status=_IDLE_STATUS,
                    clear_text=False,
                    category=failure_category,
                )
            return {
                "outcome": outcome,
                "category": category,
                "text": body,
                "connected": connected,
                "stats": {
                    "reasoning_frames": stats.reasoning_frames,
                    "tool_call_frames": stats.tool_call_frames,
                    "tool_result_frames": stats.tool_result_frames,
                    "usage_frames": stats.usage_frames,
                    "ping_frames": stats.ping_frames,
                    "delta_frames": stats.delta_frames,
                    "delta_chars": stats.delta_chars,
                },
            }
    except AgentGatewayError as exc:
        stop_event.set()
        flush_buffer(force=True)
        flush_reasoning(force=True)
        safe = safe_agent_error(exc)
        cat = str(safe.get("category") or "unknown")
        _registry.set_category(conversation_id, cat)
        # Preserve anything already produced (answer and/or thinking) so a
        # transport failure does not discard the visible thought process.
        if collected or reasoning_parts:
            _finish_turn(
                conversation_id,
                assistant_text="".join(collected),
                reasoning="".join(reasoning_parts),
            )
        else:
            _release_conversation(conversation_id)
        agent_stream.fail(
            conversation_id,
            message=str(exc),
            status=_IDLE_STATUS,
            clear_text=False,
            category=cat,
        )
        logger.warning("Agent 轮次失败：%s", cat)
        return {"outcome": "failed", "category": cat, "text": "".join(collected)}
    except Exception:  # pragma: no cover - defensive
        stop_event.set()
        logger.exception("Agent 轮次发生未预期错误。")
        _registry.set_category(conversation_id, "unknown")
        if collected or reasoning_parts:
            _finish_turn(
                conversation_id,
                assistant_text="".join(collected),
                reasoning="".join(reasoning_parts),
            )
        else:
            _release_conversation(conversation_id)
        agent_stream.fail(
            conversation_id,
            message=agent_gateway._category_message("unknown"),
            status=_IDLE_STATUS,
            clear_text=False,
            category="unknown",
        )
        return {"outcome": "failed", "category": "unknown", "text": "".join(collected)}
    finally:
        stop_event.set()


def _turn_worker(
    conversation_id: str, transport: Optional[httpx.BaseTransport], handle: TurnHandle
) -> None:
    try:
        run_turn(conversation_id, transport=transport, handle=handle)
    finally:
        _registry.pop(conversation_id)


def start_turn(
    conversation_id: str,
    *,
    stream_id: int,
    transport: Optional[httpx.BaseTransport] = None,
) -> TurnHandle:
    """Create the live preview and start the background turn thread.

    The caller has already committed status = busy; this only wires the broker
    stream id and spawns the daemon worker (the task_jobs pattern).
    """

    handle = TurnHandle(conversation_id, int(stream_id))
    _registry.register(handle)
    thread = threading.Thread(
        target=_turn_worker,
        args=(str(conversation_id), transport, handle),
        name="agent-turn-" + str(conversation_id)[:8],
        daemon=True,
    )
    handle.thread = thread
    thread.start()
    return handle
