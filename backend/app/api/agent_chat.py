"""Agent chat REST + SSE endpoints (line A of the DSH integration).

These routes are deliberately thin: conversation ownership, optimistic locking
and idempotency live here, while the DSH conversation, the turn runner and the
save-as-draft import live in ``app.services``.  Three rules inherited from the
task plane apply unchanged:

- the SSE handler is a ``sync def`` whose inner ``event_stream()`` is the async
  generator, and that generator never opens a database session (it polls the
  in-memory broker only);
- the first frame is always a complete snapshot, so a reconnecting browser can
  replace its local preview instead of replaying deltas;
- an idle conversation returns 409 instead of a stream that could only emit
  heartbeats (do not manufacture a stream that can only heartbeat).

``/api/agent`` is intentionally absent from ``_PROJECT_SCOPED_PREFIXES``, so
every handler validates conversation ownership itself (cookie session plus the
``project_id`` query parameter).  ``validate_request_project_scope`` carries a
matching rule as defence in depth.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlmodel import Session, col, select

from app import config
from app.db import get_engine, get_session
from app.models.entities import AgentConversation, AgentMessage
from app.schemas.agent import (
    AgentCancelOut,
    AgentConversationCreate,
    AgentConversationDetail,
    AgentConversationOut,
    AgentConversationUpdate,
    AgentDeleteOut,
    AgentDraftPointGroupIn,  # noqa: F401  (re-exported for the draft contract)
    AgentMessageAccepted,
    AgentMessageCreate,
    AgentMessageOut,
    AgentSaveAsDraftOut,
    AgentSkillOut,
)
from app.services import agent_draft, agent_gateway, agent_skills, agent_turn
from app.services.agent_stream import agent_stream, encode_sse
from app.services.case_management import CaseDraftParseError
from app.services.projects import request_project_id

# Re-exported so callers can import the draft contract from one place.
__all__ = ["router", "AgentDraftPointGroupIn"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

# Statuses of AgentConversation.  ``busy`` is the only streamable one; a
# retained terminal broker frame is still served so a browser that reloads
# mid-cancel recovers the failed snapshot instead of losing the reason.
_BUSY_STATUS = "busy"
_IDLE_STATUS = "active"
_CLOSED_STATUS = "closed"

_STREAM_HEARTBEAT_SEC = 15.0
_STREAM_POLL_INTERVAL_SEC = 0.2

_DEFAULT_TITLE = "未命名对话"
_TITLE_FROM_MESSAGE_CHARS = 40
_MAX_LIST_ROWS = 200

_DETAIL_DISABLED = "Agent 功能未启用（AGENT_ENABLED=false）"
_DETAIL_NOT_FOUND = "Agent 会话不存在。"
_DETAIL_FOREIGN = "Resource not found in project"
_DETAIL_BUSY = "本轮对话仍在进行中，请等待结束或先取消。"
_DETAIL_CLOSED = "会话已关闭，无法继续对话。"
_DETAIL_VERSION = "会话已更新，请刷新后重试。"
_DETAIL_CANCEL_IDLE = "当前没有进行中的对话轮次。"
_DETAIL_CANCEL_OK = "已请求取消，Agent 停止后会推送本轮终态帧。"
_DETAIL_DELETE_BUSY = "本轮对话仍在进行中，请先取消再删除会话。"
_DETAIL_DELETE_OK = "会话已删除。"
_DETAIL_STREAM_IDLE = "当前没有进行中的 Agent 轮次。"
_DETAIL_STREAM_UNAVAILABLE = "Agent 实时流不可用，请通过会话详情轮询。"
_DETAIL_DRAFT_BUSY = "本轮对话仍在进行中，请等待结束后再导入草稿。"
_DETAIL_DRAFT_FALLBACK = (
    "用例草稿导入失败，未产生任何用例。请让 Agent 修正格式后重试，或导出 Markdown 手动整理。"
)


def _require_agent_enabled() -> None:
    """Gate every /api/agent route so an operator can switch the plane off."""

    if not bool(config.AGENT_ENABLED):
        raise HTTPException(status_code=410, detail=_DETAIL_DISABLED)


def _require_project(request: Request) -> int:
    """Resolve the project the caller acts in (cookie session plus query)."""

    project_id = request_project_id(request)
    if project_id is None:
        raise HTTPException(status_code=422, detail="project_id is required")
    return int(project_id)


def _message_counts(session: Session, conversation_ids: list[str]) -> dict[str, int]:
    if not conversation_ids:
        return {}
    rows = session.exec(
        select(AgentMessage.conversation_id, func.count(col(AgentMessage.id)))
        .where(col(AgentMessage.conversation_id).in_(conversation_ids))
        .group_by(col(AgentMessage.conversation_id))
    ).all()
    return {str(key): int(value or 0) for key, value in rows}


def _conversation_out(
    session: Session, conversation: AgentConversation, *, message_count: Optional[int] = None
) -> AgentConversationOut:
    payload = AgentConversationOut.model_validate(conversation)
    if message_count is None:
        counts = _message_counts(session, [str(conversation.id)])
        message_count = counts.get(str(conversation.id), 0)
    payload.message_count = int(message_count)
    return payload


def _get_conversation(
    session: Session, conversation_id: str, project_id: Optional[int]
) -> AgentConversation:
    """Load one conversation and enforce project ownership (404 when foreign)."""

    conversation = session.get(AgentConversation, str(conversation_id))
    if conversation is None:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    if project_id is not None and str(conversation.project_id) != str(project_id):
        # Same wording as the project middleware so the browser cannot tell a
        # foreign conversation apart from a missing one.
        raise HTTPException(status_code=404, detail=_DETAIL_FOREIGN)
    return conversation


def _normalize_title(value: Optional[str]) -> str:
    text = " ".join(str(value or "").split())
    return text[:200] or _DEFAULT_TITLE


def _title_from_message(content: str) -> str:
    text = " ".join(str(content or "").split())
    if not text:
        return _DEFAULT_TITLE
    return text[:_TITLE_FROM_MESSAGE_CHARS]


def _list_conversations(
    session: Session, project_id: int
) -> list[AgentConversationOut]:
    rows = session.exec(
        select(AgentConversation)
        .where(col(AgentConversation.project_id) == str(project_id))
        .order_by(col(AgentConversation.updated_at).desc(), col(AgentConversation.id).desc())
        .limit(_MAX_LIST_ROWS)
    ).all()
    counts = _message_counts(session, [str(row.id) for row in rows])
    return [
        _conversation_out(session, row, message_count=counts.get(str(row.id), 0))
        for row in rows
    ]


def _load_messages(session: Session, conversation_id: str) -> list[AgentMessageOut]:
    rows = session.exec(
        select(AgentMessage)
        .where(col(AgentMessage.conversation_id) == str(conversation_id))
        .order_by(col(AgentMessage.created_at), col(AgentMessage.id))
    ).all()
    return [AgentMessageOut.model_validate(row) for row in rows]


def _duplicate_message(
    session: Session, conversation_id: str, idempotency_key: str
) -> Optional[AgentMessage]:
    return session.exec(
        select(AgentMessage)
        .where(col(AgentMessage.conversation_id) == str(conversation_id))
        .where(col(AgentMessage.idempotency_key) == idempotency_key)
        .order_by(col(AgentMessage.created_at))
        .limit(1)
    ).first()

def _check_version(conversation: AgentConversation, expected_version: Optional[int]) -> None:
    """Optimistic lock: reject a write issued from a stale conversation view."""

    if expected_version is None:
        return
    if int(conversation.version or 1) != int(expected_version):
        raise HTTPException(status_code=409, detail=_DETAIL_VERSION)


def _cleanup_workspace(workspace_dir: str) -> None:
    """Remove one agent scratch directory (best effort, never raises).

    Only a directory strictly under the configured workspace root is removed,
    so a stale or hand-edited row can never delete files outside
    ``data/agent-workspace``.  The application database lives in ``data/meta``,
    a sibling of the root, and is therefore unreachable by construction.
    """

    text = str(workspace_dir or "").strip()
    if not text:
        return
    try:
        root = Path(config.agent_workspace_root()).resolve()
        target = Path(text).resolve()
        target.relative_to(root)
    except (OSError, ValueError):
        logger.warning("跳过工作区根目录之外的 Agent 目录清理。")
        return
    if target == root:
        return
    shutil.rmtree(target, ignore_errors=True)


def _close_dsh_session(dsh_session_id: str, dsh_workspace_id: str) -> None:
    """Release DSH-side rows for a deleted conversation (best effort)."""

    if str(dsh_session_id or "").strip():
        agent_gateway.delete_session(dsh_session_id)
    if str(dsh_workspace_id or "").strip():
        agent_gateway.delete_workspace(dsh_workspace_id)


def _recover_orphaned_turn(session: Session, conversation: AgentConversation) -> None:
    """Return a conversation stuck in ``busy`` to idle after a restart.

    Agent turns live in worker threads, so a process restart loses them without
    leaving a durable trace (there is no job queue on this plane).  A row is
    only recovered when no worker handle and no broker stream remain, and the
    check runs under the per-conversation lock that ``post_message`` holds from
    before its commit until the live preview exists, so a turn that is starting
    right now can never be mistaken for an orphan.
    """

    if conversation.status != _BUSY_STATUS:
        return
    conversation_id = str(conversation.id)
    if conversation_id in set(agent_turn.active_turn_ids()):
        return
    if agent_stream.snapshot(conversation_id) is not None:
        return
    with agent_gateway.conversation_locks.hold(conversation_id):
        session.expire_all()
        row = session.get(AgentConversation, conversation_id)
        if row is None or row.status != _BUSY_STATUS:
            return
        if conversation_id in set(agent_turn.active_turn_ids()):
            return
        if agent_stream.snapshot(conversation_id) is not None:
            return
        row.status = _IDLE_STATUS
        row.cancel_requested = False
        row.version = int(row.version or 1) + 1
        session.add(row)
        session.commit()
        session.refresh(row)
    logger.warning("检测到无主的 Agent 轮次，已恢复会话为空闲：%s", conversation_id)



@router.get("/conversations", response_model=list[AgentConversationOut])
def list_conversations(
    request: Request,
    session: Session = Depends(get_session),
) -> list[AgentConversationOut]:
    """List this project's conversations, newest activity first (bare array)."""

    _require_agent_enabled()
    return _list_conversations(session, _require_project(request))


@router.post("/conversations", response_model=AgentConversationOut)
def create_conversation(
    body: AgentConversationCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> AgentConversationOut:
    """Create an idle conversation row.  No DSH session is created yet."""

    _require_agent_enabled()
    project_id = _require_project(request)
    conversation = AgentConversation(
        project_id=str(project_id),
        title=_normalize_title(body.title),
        skill_name=(body.skill_name or "").strip() or None,
        model=(body.model or "").strip() or None,
        workspace_dir="",
        status=_IDLE_STATUS,
    )
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    logger.info("Agent 会话已创建：project=%s", project_id)
    return _conversation_out(session, conversation)


@router.get("/conversations/{conversation_id}", response_model=AgentConversationDetail)
def get_conversation(
    conversation_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> AgentConversationDetail:
    """Flat conversation detail: conversation fields plus ``messages``.

    This is also the durable polling fallback for the SSE stream, so it must
    stay cheap and always answer (no broker dependency).
    """

    _require_agent_enabled()
    conversation = _get_conversation(session, conversation_id, _require_project(request))
    _recover_orphaned_turn(session, conversation)
    detail = AgentConversationDetail.model_validate(conversation)
    detail.messages = _load_messages(session, conversation_id)
    detail.message_count = len(detail.messages)
    return detail


@router.patch("/conversations/{conversation_id}", response_model=AgentConversationOut)
def update_conversation(
    conversation_id: str,
    body: AgentConversationUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> AgentConversationOut:
    """Rename a conversation or switch its skill/model for the next turn."""

    _require_agent_enabled()
    conversation = _get_conversation(session, conversation_id, _require_project(request))
    _check_version(conversation, body.expected_version)
    if body.title is not None and " ".join(body.title.split()):
        conversation.title = _normalize_title(body.title)
    if body.skill_name is not None:
        conversation.skill_name = body.skill_name.strip() or None
    if body.model is not None:
        conversation.model = body.model.strip() or None
    conversation.version = int(conversation.version or 1) + 1
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    return _conversation_out(session, conversation)


@router.delete("/conversations/{conversation_id}", response_model=AgentDeleteOut)
def delete_conversation(
    conversation_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> AgentDeleteOut:
    """Delete a conversation, its messages and its broker stream.

    The DSH workspace directory under ``data/agent-workspace/<conv_id>/`` is
    removed best-effort: it only ever holds agent scratch files, never the
    application database.  A busy conversation must be cancelled first.
    """

    _require_agent_enabled()
    project_id = _require_project(request)
    with agent_gateway.conversation_locks.hold(conversation_id):
        session.expire_all()
        conversation = _get_conversation(session, conversation_id, project_id)
        if conversation.status == _BUSY_STATUS:
            raise HTTPException(status_code=409, detail=_DETAIL_DELETE_BUSY)
        workspace_dir = conversation.workspace_dir
        dsh_session_id = conversation.dsh_session_id
        dsh_workspace_id = conversation.dsh_workspace_id
        messages = session.exec(
            select(AgentMessage).where(col(AgentMessage.conversation_id) == str(conversation_id))
        ).all()
        for message in messages:
            session.delete(message)
        session.delete(conversation)
        session.commit()
    # Publish after the commit so a subscriber never sees a stream for a
    # conversation that no longer exists.
    agent_stream.discard(conversation_id)
    _cleanup_workspace(workspace_dir)
    _close_dsh_session(dsh_session_id or "", dsh_workspace_id or "")
    return AgentDeleteOut(id=str(conversation_id), status="deleted", message=_DETAIL_DELETE_OK)


@router.post("/conversations/{conversation_id}/messages", response_model=AgentMessageAccepted)
def post_message(
    conversation_id: str,
    body: AgentMessageCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> AgentMessageAccepted:
    """Append one user message and start the agent turn in a worker thread.

    ``idempotency_key`` makes a retried POST safe: a duplicate returns the
    already-stored message with ``duplicated=true`` and starts no second turn
    (a repeated DSH prompt would re-run the whole turn).  ``expected_version``
    is the optimistic lock against a stale editor.
    """

    _require_agent_enabled()
    project_id = _require_project(request)
    idempotency_key = body.idempotency_key.strip()
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="消息内容不能为空。")

    started_stream_id: Optional[int] = None
    with agent_gateway.conversation_locks.hold(conversation_id):
        # Another request may have committed a turn while this one waited.
        session.expire_all()
        conversation = _get_conversation(session, conversation_id, project_id)
        if conversation.status == _BUSY_STATUS:
            raise HTTPException(status_code=409, detail=_DETAIL_BUSY)
        if conversation.status == _CLOSED_STATUS:
            raise HTTPException(status_code=409, detail=_DETAIL_CLOSED)
        _check_version(conversation, body.expected_version)

        duplicate = _duplicate_message(session, conversation_id, idempotency_key)
        if duplicate is not None:
            message = AgentMessageOut.model_validate(duplicate)
            conversation_out = _conversation_out(session, conversation)
            return AgentMessageAccepted(
                conversation=conversation_out, message=message, duplicated=True
            )

        message = AgentMessage(
            conversation_id=str(conversation_id),
            role="user",
            content=content,
            idempotency_key=idempotency_key,
        )
        session.add(message)
        if conversation.title == _DEFAULT_TITLE:
            conversation.title = _title_from_message(content)
        conversation.status = _BUSY_STATUS
        conversation.cancel_requested = False
        conversation.version = int(conversation.version or 1) + 1
        session.add(conversation)
        session.commit()
        session.refresh(message)
        session.refresh(conversation)
        # Create the live preview before the worker starts, exactly like
        # _start_live_generate_stream on the task plane: the browser can then
        # subscribe and always receives a snapshot first frame.
        agent_stream.start(
            conversation_id,
            status=conversation.status,
            message=agent_turn.STATUS_CONNECTING,
        )
        snapshot = agent_stream.snapshot(conversation_id) or {}
        started_stream_id = int(snapshot.get("stream_id") or 0)
        message_out = AgentMessageOut.model_validate(message)
        conversation_out = _conversation_out(session, conversation)

    agent_turn.start_turn(conversation_id, stream_id=started_stream_id or 0)
    return AgentMessageAccepted(
        conversation=conversation_out, message=message_out, duplicated=False
    )


@router.post("/conversations/{conversation_id}/cancel", response_model=AgentCancelOut)
def cancel_turn(
    conversation_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> AgentCancelOut:
    """Ask the running turn to stop (cooperative).

    The flag is durable; the worker polls it between DSH frames, tells DSH to
    cancel and then publishes a terminal frame before the stream closes, so the
    browser never stays stuck in busy.  Cancelling an idle conversation is a
    soft no-op (200, ``cancelled=false``) rather than the task plane's 409: the
    frontend fires this on unmount and a race with a finished turn is normal.
    """

    _require_agent_enabled()
    project_id = _require_project(request)
    conversation = _get_conversation(session, conversation_id, project_id)
    cancelled = agent_turn.request_cancel(conversation_id)
    session.expire_all()
    conversation = _get_conversation(session, conversation_id, project_id)
    return AgentCancelOut(
        conversation=_conversation_out(session, conversation),
        cancelled=cancelled,
        message=_DETAIL_CANCEL_OK if cancelled else _DETAIL_CANCEL_IDLE,
    )


@router.get("/skills", response_model=list[AgentSkillOut])
def list_skills(
    request: Request,
) -> list[AgentSkillOut]:
    """List repo skills (``.agents/skills/*/SKILL.md``) as a bare array."""

    _require_agent_enabled()
    # Project scope is still required so an unauthenticated or cross-project
    # caller cannot enumerate the deployment; the list itself is repo-global.
    _require_project(request)
    return [AgentSkillOut(**item.as_dict()) for item in agent_skills.list_skills()]

def _load_conversation_status(conversation_id: str, project_id: Optional[int]) -> str:
    """Load only durable stream eligibility and close the DB session eagerly."""

    with Session(get_engine()) as session:
        conversation = _get_conversation(session, conversation_id, project_id)
        return str(conversation.status or _IDLE_STATUS)


@router.get("/conversations/{conversation_id}/stream")
def stream_conversation(
    conversation_id: str,
    request: Request,
) -> StreamingResponse:
    """Subscribe to the live preview of one agent turn.

    The first frame is always a complete snapshot, so a reconnecting browser
    replaces its local preview instead of replaying deltas.  The generator
    polls the in-memory broker only: it never holds a database session while
    the model is producing output, and it never opens one at all.
    """

    _require_agent_enabled()
    project_id = _require_project(request)
    conversation_status = _load_conversation_status(conversation_id, project_id)
    snapshot = agent_stream.snapshot(conversation_id)
    durable_terminal: Optional[str] = None
    if snapshot is not None:
        durable_terminal = snapshot.get("terminal")
    if snapshot is None and conversation_status == _BUSY_STATUS:
        # A restart or broker eviction lost the live producer.  Do not
        # manufacture a stream that can only heartbeat; the durable
        # conversation detail remains the authoritative polling fallback.
        raise HTTPException(status_code=409, detail=_DETAIL_STREAM_UNAVAILABLE)
    if conversation_status != _BUSY_STATUS and durable_terminal is None:
        raise HTTPException(status_code=409, detail=_DETAIL_STREAM_IDLE)
    if snapshot is None:
        snapshot = {
            "stream_id": 0,
            "sequence": 0,
            "status": conversation_status,
            "text": "",
            "reasoning": "",
            "terminal": None,
            "truncated": False,
        }

    async def event_stream() -> AsyncIterator[str]:
        sequence = int(snapshot.get("sequence") or 0)
        stream_id = int(snapshot.get("stream_id") or 0)
        next_heartbeat_at = time.monotonic() + _STREAM_HEARTBEAT_SEC
        yield encode_sse("snapshot", dict(snapshot), sequence=sequence)
        if snapshot.get("terminal") in {"completed", "failed"}:
            return
        while True:
            result = agent_stream.poll_after(
                conversation_id, sequence, stream_id=stream_id
            )
            if result.kind == "missing":
                # Evicted or expired: close the stream and let EventSource
                # reconnect, where the durable status is authoritative again.
                return
            if result.kind == "timeout":
                now = time.monotonic()
                if now >= next_heartbeat_at:
                    yield ": keep-alive\n\n"
                    next_heartbeat_at = now + _STREAM_HEARTBEAT_SEC
                await asyncio.sleep(_STREAM_POLL_INTERVAL_SEC)
                continue
            if result.kind == "snapshot":
                current = result.snapshot or {}
                sequence = int(current.get("sequence") or sequence)
                stream_id = int(current.get("stream_id") or stream_id)
                yield encode_sse("snapshot", dict(current), sequence=sequence)
                if current.get("terminal") in {"completed", "failed"}:
                    return
                continue
            for item in result.events:
                sequence = int(item.sequence)
                yield encode_sse(item.event, item.payload, sequence=sequence)
                if item.event in {"completed", "failed"}:
                    return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/conversations/{conversation_id}/save-as-draft",
    response_model=AgentSaveAsDraftOut,
)
def save_as_draft(
    conversation_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> AgentSaveAsDraftOut:
    """Import the latest agent answer as a case draft plus TestCase rows.

    Spec 3.5 plan a: a virtual Requirement carries a shell GenerationTask set
    straight to ``generated`` (the agent has no test-point checkpoint chain, so
    neither the task state machine nor ``finalize_task`` applies), a CaseDraft
    version is written from the rendered Markdown and
    ``import_cases_from_draft`` produces the cases.  The whole thing is atomic:
    a malformed answer leaves the conversation retryable and never commits a
    partial case set.
    """

    _require_agent_enabled()
    project_id = _require_project(request)
    conversation = _get_conversation(session, conversation_id, project_id)
    if conversation.status == _BUSY_STATUS:
        raise HTTPException(status_code=409, detail=_DETAIL_DRAFT_BUSY)
    try:
        result = agent_draft.save_conversation_as_draft(session, conversation)
    except agent_draft.AgentDraftError as exc:
        session.rollback()
        code = str(getattr(exc, "code", "") or "")
        raise HTTPException(
            status_code=409 if code == "conflict" else 422, detail=str(exc)
        ) from None
    except CaseDraftParseError:
        session.rollback()
        logger.warning(
            "Agent 草稿导入解析失败：conversation=%s", conversation_id
        )
        raise HTTPException(
            status_code=422, detail=_DETAIL_DRAFT_FALLBACK
        ) from None
    except Exception:
        session.rollback()
        logger.exception(
            "Agent 草稿导入发生未预期错误：conversation=%s", conversation_id
        )
        raise HTTPException(
            status_code=422, detail=_DETAIL_DRAFT_FALLBACK
        ) from None
    logger.info(
        "Agent 对话已导入用例草稿：conversation=%s task=%s count=%s",
        conversation_id,
        result.get("task_id"),
        result.get("imported_count"),
    )
    return AgentSaveAsDraftOut(**result)


