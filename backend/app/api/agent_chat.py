"""Agent chat REST + SSE endpoints (line A of the DSH integration).

These routes are deliberately thin: conversation ownership, optimistic locking
and idempotency live here, while the DSH conversation, the turn runner and the
direct TestCase import live in ``app.services``.  Three rules inherited from the
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

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app import config
from app.db import get_engine, get_session
from app.models.entities import AgentConversation, AgentMessage, Project
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
    AgentAttachmentOut,
    AgentModelItem,
    AgentModelsOut,
    AgentSaveCasesIn,
    AgentSaveCasesOut,
    AgentSkillOut,
)
from app.services import agent_draft, agent_attachments, agent_gateway, agent_skills, agent_turn
from app.services.agent_gateway import AgentGatewayError
from app.services.agent_stream import agent_stream
from app.services.stream_broker import build_sse_streaming_response
from app.services.case_management import CaseDraftParseError
from app.services.permissions import current_user
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
# Single neutral 404 wording for missing, cross-project and foreign-owner
# conversations.  Keeping them identical prevents existence enumeration and
# matches the project middleware's "Resource not found in project".
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
_DETAIL_DRAFT_BUSY = "本轮对话仍在进行中，请等待结束后再入库用例。"
_DETAIL_DRAFT_FALLBACK = (
    "用例入库失败，未产生任何用例。请让 Agent 修正格式后重试，或导出 Markdown 手动整理。"
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


def _owner_id(request: Request) -> Optional[int]:
    """Return the signed-in user's id, failing closed when auth is enabled.

    ``tests/conftest.py`` turns authentication off for legacy pipeline tests.
    When ``AUTH_ENABLED`` is true, a missing user must raise 401 rather than
    silently skipping ownership filters.
    """

    user = current_user(request)
    if user is not None and user.id is not None:
        return int(user.id)
    if bool(config.AUTH_ENABLED):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return None


def _project_workspace_dir(session: Session, project_id: int, conversation: AgentConversation) -> str:
    project = session.get(Project, project_id)
    return str((project.workspace_dir if project is not None else "") or conversation.workspace_dir or "")


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
    session: Session,
    conversation_id: str,
    project_id: Optional[int],
    owner_id: Optional[int] = None,
) -> AgentConversation:
    """Load one conversation and enforce project + user ownership (404).

    Conversations are private to their creator; even an administrator cannot
    open another user's chat.  ``owner_id is None`` means the compatibility
    mode (auth disabled) and skips only the user check, never the project one.
    All rejection branches return the same neutral 404 so the response never
    reveals whether a given conversation exists.
    """

    conversation = session.get(AgentConversation, str(conversation_id))
    # Every rejection below uses the SAME neutral 404 detail.  A caller must
    # not be able to enumerate whether another user's conversation exists by
    # comparing error messages for "missing" vs "foreign project" vs
    # "foreign owner"; all three are indistinguishable from a missing row.
    # The wording also matches the project middleware's 404.
    if conversation is None:
        raise HTTPException(status_code=404, detail=_DETAIL_FOREIGN)
    if project_id is not None and str(conversation.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_FOREIGN)
    if owner_id is not None and conversation.user_id != owner_id:
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
    session: Session, project_id: int, owner_id: Optional[int] = None
) -> list[AgentConversationOut]:
    query = select(AgentConversation).where(
        col(AgentConversation.project_id) == str(project_id)
    )
    if owner_id is not None:
        query = query.where(col(AgentConversation.user_id) == owner_id)
    rows = session.exec(
        query.order_by(
            col(AgentConversation.updated_at).desc(),
            col(AgentConversation.id).desc(),
        ).limit(_MAX_LIST_ROWS)
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
    return [_message_out(row) for row in rows]


def _message_out(row: AgentMessage) -> AgentMessageOut:
    try:
        raw = json.loads(row.attachments_json or "[]")
        items = [AgentAttachmentOut(**{key: item[key] for key in ("id", "filename", "size", "content_type", "char_count", "created_at")})
                 for item in raw if isinstance(item, dict)]
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        items = []
    return AgentMessageOut.model_validate(row).model_copy(update={"attachments": items})


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


def _close_dsh_session(dsh_session_id: str) -> None:
    """Release DSH-side session for a deleted conversation (best effort)."""

    if str(dsh_session_id or "").strip():
        agent_gateway.delete_session(dsh_session_id)


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



@router.get("/models", response_model=AgentModelsOut)
def list_agent_models(request: Request) -> AgentModelsOut:
    """Proxy the DSH model catalog for the Agent model picker.

    CaseGen /api/models configures the *legacy* LLM pipeline; Agent chat
    runs on the DSH plane and needs DSH own provider/model ids. A failed
    DSH call is surfaced as 502 instead of an empty list, so the UI never
    silently offers wrong candidates.
    """

    _require_agent_enabled()
    _require_project(request)
    try:
        data = agent_gateway.list_models()
    except AgentGatewayError as exc:
        raise HTTPException(
            status_code=502,
            detail="无法获取 DSH 模型列表：" + str(exc),
        ) from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="DSH 模型列表格式无效")

    raw_models = data.get("models")
    models: list[AgentModelItem] = []
    if isinstance(raw_models, list):
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "").strip()
            model_id = str(item.get("id") or "").strip()
            if not provider or not model_id:
                continue
            models.append(
                AgentModelItem(
                    id=model_id,
                    provider=provider,
                    name=str(item.get("name") or model_id),
                    is_default=bool(item.get("isDefault")),
                    value=provider + "/" + model_id,
                )
            )

    default = data.get("defaultModel")
    default = default if isinstance(default, dict) else {}
    default_provider = str(default.get("provider") or "").strip()
    default_model = str(default.get("model") or "").strip()
    default_effort = str(default.get("reasoningEffort") or "").strip()
    default_value = default_provider + "/" + default_model if default_provider and default_model else ""
    if default_value:
        for item in models:
            if item.value == default_value:
                item.is_default = True
    return AgentModelsOut(
        models=models,
        default_provider=default_provider,
        default_model=default_model,
        default_reasoning_effort=default_effort,
    )

@router.get("/conversations", response_model=list[AgentConversationOut])
def list_conversations(
    request: Request,
    session: Session = Depends(get_session),
) -> list[AgentConversationOut]:
    """List this project's conversations, newest activity first (bare array)."""

    _require_agent_enabled()
    return _list_conversations(session, _require_project(request), _owner_id(request))


@router.post("/conversations", response_model=AgentConversationOut)
def create_conversation(
    body: AgentConversationCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> AgentConversationOut:
    """Create an idle conversation row.  No DSH session is created yet."""

    _require_agent_enabled()
    project_id = _require_project(request)
    project = session.get(Project, project_id)
    ws_id = project.dsh_workspace_id if project else None
    ws_dir = project.workspace_dir if project else ""
    conversation = AgentConversation(
        project_id=str(project_id),
        user_id=_owner_id(request),
        title=_normalize_title(body.title),
        skill_name=(body.skill_name or "").strip() or None,
        model=(body.model or "").strip() or None,
        dsh_workspace_id=ws_id,
        workspace_dir=ws_dir,
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
    conversation = _get_conversation(session, conversation_id, _require_project(request), _owner_id(request))
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
    conversation = _get_conversation(session, conversation_id, _require_project(request), _owner_id(request))
    _check_version(conversation, body.expected_version)
    if body.title is not None and " ".join(body.title.split()):
        conversation.title = _normalize_title(body.title)
    if body.skill_name is not None:
        conversation.skill_name = body.skill_name.strip() or None
    model_changed = False
    # model_fields_set distinguishes "field omitted" from an explicit JSON
    # null.  The frontend sends null when the picker is cleared, which must
    # reset the conversation to the DSH default model.  An empty string is
    # treated the same way for callers that use "" instead of null.
    if "model" in body.model_fields_set:
        next_model = (body.model or "").strip() or None
        if next_model != conversation.model:
            model_changed = True
        conversation.model = next_model
    conversation.version = int(conversation.version or 1) + 1
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    if model_changed and str(conversation.dsh_session_id or "").strip():
        ok = agent_gateway.update_session_model(
            str(conversation.dsh_session_id), conversation.model
        )
        if not ok:
            # Never report success while DSH silently kept the old model.
            raise HTTPException(
                status_code=502,
                detail="模型切换失败，请稍后重试或新建对话。",
            )
    return _conversation_out(session, conversation)


@router.delete("/conversations/{conversation_id}", response_model=AgentDeleteOut)
def delete_conversation(
    conversation_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> AgentDeleteOut:
    """Delete a conversation, its messages and its broker stream.

    Conversation attachments are removed best-effort while preserving the shared
    project workspace directory.  A busy conversation must be cancelled first.
    """

    _require_agent_enabled()
    project_id = _require_project(request)
    with agent_gateway.conversation_locks.hold(conversation_id):
        session.expire_all()
        conversation = _get_conversation(session, conversation_id, project_id, _owner_id(request))
        if conversation.status == _BUSY_STATUS:
            raise HTTPException(status_code=409, detail=_DETAIL_DELETE_BUSY)
        dsh_session_id = conversation.dsh_session_id
        workspace_dir = _project_workspace_dir(session, project_id, conversation)
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
    # Only close the session; shared project workspace & dir are preserved.
    _close_dsh_session(dsh_session_id or "")
    try:
        agent_attachments.delete_conversation_attachments(project_id=project_id, conversation_id=conversation_id,
                                                           workspace_dir=workspace_dir)
    except Exception:
        logger.warning("清理 Agent 附件失败：conversation=%s", conversation_id, exc_info=True)
    return AgentDeleteOut(id=str(conversation_id), status="deleted", message=_DETAIL_DELETE_OK)


@router.post("/conversations/{conversation_id}/attachments", response_model=AgentAttachmentOut)
def upload_attachment(conversation_id: str, request: Request, file: UploadFile = File(...),
                      session: Session = Depends(get_session)) -> AgentAttachmentOut:
    _require_agent_enabled()
    project_id = _require_project(request)
    conversation = _get_conversation(session, conversation_id, project_id, _owner_id(request))
    workspace_dir = _project_workspace_dir(session, project_id, conversation)
    if conversation.status == _BUSY_STATUS:
        raise HTTPException(status_code=409, detail=_DETAIL_BUSY)
    if conversation.status == _CLOSED_STATUS:
        raise HTTPException(status_code=409, detail=_DETAIL_CLOSED)
    try:
        content = file.file.read(config.MAX_UPLOAD_BYTES + 1)
        meta = agent_attachments.save_attachment(project_id=project_id, conversation_id=conversation_id,
            filename=file.filename or "", content=content, content_type=file.content_type,
            workspace_dir=workspace_dir)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        logger.exception("Agent 附件解析失败：conversation=%s", conversation_id)
        raise HTTPException(status_code=422, detail="附件解析失败，未保存附件。") from None
    return AgentAttachmentOut(**{key: meta[key] for key in ("id", "filename", "size", "content_type", "char_count", "created_at")})


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

    started_stream_id: Optional[int] = None
    with agent_gateway.conversation_locks.hold(conversation_id):
        # Another request may have committed a turn while this one waited.
        session.expire_all()
        conversation = _get_conversation(session, conversation_id, project_id, _owner_id(request))
        if conversation.status == _BUSY_STATUS:
            raise HTTPException(status_code=409, detail=_DETAIL_BUSY)
        if conversation.status == _CLOSED_STATUS:
            raise HTTPException(status_code=409, detail=_DETAIL_CLOSED)
        _check_version(conversation, body.expected_version)
        duplicate = _duplicate_message(session, conversation_id, idempotency_key)
        if duplicate is not None:
            message = _message_out(duplicate)
            conversation_out = _conversation_out(session, conversation)
            return AgentMessageAccepted(
                conversation=conversation_out, message=message, duplicated=True
            )

        if not content and not body.attachment_ids:
            raise HTTPException(status_code=422, detail="消息内容不能为空。")

        workspace_dir = _project_workspace_dir(session, project_id, conversation)
        try:
            attachments = agent_attachments.resolve_attachments(project_id=project_id, conversation_id=conversation_id,
                attachment_ids=body.attachment_ids, workspace_dir=workspace_dir)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

        message = AgentMessage(
            conversation_id=str(conversation_id),
            role="user",
            content=content,
            attachments_json=json.dumps(attachments, ensure_ascii=False),
            idempotency_key=idempotency_key,
        )
        session.add(message)
        if conversation.title == _DEFAULT_TITLE:
            conversation.title = _title_from_message(content or f"附件：{attachments[0]['filename']}")
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
        message_out = _message_out(message)
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
    conversation = _get_conversation(session, conversation_id, project_id, _owner_id(request))
    cancelled = agent_turn.request_cancel(conversation_id)
    session.expire_all()
    conversation = _get_conversation(session, conversation_id, project_id, _owner_id(request))
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

def _load_conversation_status(
    conversation_id: str, project_id: Optional[int], owner_id: Optional[int]
) -> str:
    """Load only durable stream eligibility and close the DB session eagerly."""

    with Session(get_engine()) as session:
        conversation = _get_conversation(session, conversation_id, project_id, owner_id)
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
    conversation_status = _load_conversation_status(conversation_id, project_id, _owner_id(request))
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

    return build_sse_streaming_response(
        agent_stream,
        conversation_id,
        initial_snapshot=snapshot,
        poll_interval_sec=_STREAM_POLL_INTERVAL_SEC,
        heartbeat_sec=_STREAM_HEARTBEAT_SEC,
    )


@router.post(
    "/conversations/{conversation_id}/save-cases",
    response_model=AgentSaveCasesOut,
)
def save_cases(
    conversation_id: str,
    body: AgentSaveCasesIn,
    request: Request,
    session: Session = Depends(get_session),
) -> AgentSaveCasesOut:
    """Import cases from a specific assistant message directly into TestCase."""

    _require_agent_enabled()
    project_id = _require_project(request)
    owner_id = _owner_id(request)
    conversation = _get_conversation(session, conversation_id, project_id, owner_id)
    if conversation.status == _BUSY_STATUS:
        raise HTTPException(status_code=409, detail=_DETAIL_DRAFT_BUSY)

    user = current_user(request)
    operator = user.username if user is not None else None
    raw_uid = getattr(user, "id", None) if user is not None else None
    operator_user_id = int(raw_uid) if isinstance(raw_uid, int) and not isinstance(raw_uid, bool) and raw_uid > 0 else None
    try:
        result = agent_draft.save_message_cases_to_requirement(
            session,
            conversation,
            message_id=body.message_id,
            target_mode=body.target_mode,
            requirement_id=body.requirement_id,
            requirement_title=body.requirement_title,
            requirement_description=body.requirement_description,
            conflict_strategy=body.conflict_strategy,
            operator=operator,
            operator_user_id=operator_user_id,
        )
    except agent_draft.AgentDraftError as exc:
        session.rollback()
        code = str(getattr(exc, "code", "") or "")
        if code == "not_found":
            raise HTTPException(status_code=404, detail=_DETAIL_FOREIGN) from None
        if code == "conflict":
            raise HTTPException(
                status_code=409,
                detail={
                    "message": str(exc),
                    "code": "conflict",
                    "conflicts": list(getattr(exc, "conflicts", []) or []),
                },
            ) from None
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "message": "存在并发重复的用例编号，请刷新后重试。",
                "code": "conflict",
                "conflicts": [],
            },
        ) from None
    except CaseDraftParseError:
        session.rollback()
        logger.warning(
            "Agent 消息用例解析失败：conversation=%s message=%s",
            conversation_id,
            body.message_id,
        )
        raise HTTPException(
            status_code=422, detail=_DETAIL_DRAFT_FALLBACK
        ) from None
    except Exception:
        session.rollback()
        logger.exception(
            "Agent 消息直入用例库发生未预期错误：conversation=%s message=%s",
            conversation_id,
            body.message_id,
        )
        raise HTTPException(
            status_code=422, detail=_DETAIL_DRAFT_FALLBACK
        ) from None

    logger.info(
        "Agent 消息已直入用例库：conversation=%s message=%s requirement=%s count=%s",
        conversation_id,
        body.message_id,
        result.get("requirement_id"),
        result.get("imported_count"),
    )
    return AgentSaveCasesOut(**result)


