"""Shared helpers, constants, model/prompt resolution, and test-hook dispatch."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlmodel import Session, col, func, select

from app.models.entities import (
    CaseDraft,
    GenerationTask,
    ModelConfig,
    PromptTemplate,
    Requirement,
    ReviewResult,
    TaskCitation,
    TaskReferenceCase,
    TaskTestPointCheckpoint,
)
from app.services.llm import LLMError, chat_completion
from app.services.task_events import append_event
from app.services.task_state import InvalidTransition, transition
from app.services.task_stream import task_stream

ChatFn = Callable[..., Any]

# Used when a primary generation call hits a flaky gateway (502, context limit, etc.).
_LEAN_GENERATE_SYSTEM = (
    "你是金融/交易所测试专家。根据需求、Wiki 与原文块输出中文测试用例 Markdown。"
    "每条用例含：标题、优先级、类型、关联知识、条款号、前置条件、步骤、预期。"
    "必须引用 Wiki 编号[1]与/或原文[S1]，并写明规则条款号（如 3.5.2）；"
    "覆盖正常/边界/异常；不得编造未提供的规则；只输出用例 Markdown。"
)

_STAGE_HOOK_ATTRS: dict[str, str] = {
    "generate": "_GENERATE_CHAT_FN",
    "test_points": "_TEST_POINTS_CHAT_FN",
    "review": "_REVIEW_CHAT_FN",
    "optimize": "_OPTIMIZE_CHAT_FN",
}


def _get_facade():
    """Return the task_pipeline facade module if loaded, without causing circular imports."""
    return sys.modules.get("app.services.task_pipeline")


def _facade_hook(attr_name: str) -> Optional[ChatFn]:
    """Read an injectable chat hook dynamically from the task_pipeline facade."""
    facade = _get_facade()
    return getattr(facade, attr_name, None) if facade is not None else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _set_status(task: GenerationTask, new_status: str) -> None:
    task.status = transition(task.status, new_status)
    task.updated_at = _utcnow()


def _generation_stopped(session: Session, task: GenerationTask) -> bool:
    """Re-read the durable row to observe a manual stop issued mid-generation.

    ``POST /api/tasks/{id}/stop`` flips ``generating -> failed`` in its own
    transaction while the worker is awaiting the LLM.  ``failed`` has no
    outgoing edge to ``generated``, so a long-running worker must poll this
    between LLM calls and bail out instead of overwriting the terminal state.
    """
    session.refresh(task)
    return task.status != "generating"


def _focus_tags(requirement: Requirement) -> list[str]:
    try:
        tags = json.loads(requirement.focus_tags_json or "[]")
    except json.JSONDecodeError:
        tags = []
    if not isinstance(tags, list):
        return []
    return [str(t) for t in tags]


def _resolve_default_generate_prompt(session: Session) -> Optional[PromptTemplate]:
    """Resolve default active generate prompt: system first, then shared; never private."""
    for vis in ("system", "shared"):
        row = session.exec(
            select(PromptTemplate)
            .where(
                PromptTemplate.type == "generate",
                PromptTemplate.visibility == vis,
                PromptTemplate.is_active == True,  # noqa: E712
            )
            .order_by(col(PromptTemplate.updated_at).desc(), col(PromptTemplate.id).desc())
        ).first()
        if row is not None:
            return row
    return None


def _resolve_generate_prompt(session: Session, task: GenerationTask) -> tuple[str, str]:
    """Return (prompt_content, prompt_version_ref)."""
    if task.temp_prompt_content:
        return task.temp_prompt_content, "temp"

    if task.prompt_template_id is not None:
        row = session.get(PromptTemplate, task.prompt_template_id)
        if row is not None:
            if row.type != "generate":
                raise RuntimeError(
                    f"PromptTemplate id={row.id} has type={row.type!r}; expected 'generate'"
                )
            return row.content, f"id:{row.id}:v{row.version}"

    active = _resolve_default_generate_prompt(session)
    if active is None:
        raise RuntimeError("No active generate prompt template found")
    return active.content, f"id:{active.id}:v{active.version}"


def _resolve_prompt_by_type(session: Session, prompt_type: str) -> PromptTemplate:
    if prompt_type == "generate":
        active = _resolve_default_generate_prompt(session)
    else:
        active = session.exec(
            select(PromptTemplate)
            .where(
                PromptTemplate.type == prompt_type,
                PromptTemplate.visibility == "system",
                PromptTemplate.is_active == True,  # noqa: E712
            )
            .order_by(col(PromptTemplate.updated_at).desc(), col(PromptTemplate.id).desc())
        ).first()
    if active is None:
        raise RuntimeError(f"No active {prompt_type} prompt template found")
    return active


def _resolve_model(session: Session, task: GenerationTask) -> ModelConfig:
    if task.model_id is not None:
        row = session.get(ModelConfig, task.model_id)
        if row is None:
            raise RuntimeError(f"ModelConfig id={task.model_id} not found")
        return row
    default = session.exec(
        select(ModelConfig)
        .where(ModelConfig.is_default == True)  # noqa: E712
        .order_by(col(ModelConfig.id).desc())
    ).first()
    if default is None:
        # Fall back to any model if no default is marked.
        default = session.exec(select(ModelConfig).order_by(col(ModelConfig.id).desc())).first()
    if default is None:
        raise RuntimeError("No ModelConfig available")
    return default


def _resolve_review_model(session: Session, task: GenerationTask) -> ModelConfig:
    if task.review_model_id is not None:
        row = session.get(ModelConfig, task.review_model_id)
        if row is None:
            raise RuntimeError(f"ModelConfig id={task.review_model_id} not found")
        return row
    return _resolve_model(session, task)


def _latest_draft(session: Session, task_id: int) -> Optional[CaseDraft]:
    return session.exec(
        select(CaseDraft)
        .where(CaseDraft.task_id == task_id)
        .order_by(col(CaseDraft.version).desc())
    ).first()


def _latest_review(session: Session, task_id: int) -> Optional[ReviewResult]:
    return session.exec(
        select(ReviewResult)
        .where(ReviewResult.task_id == task_id)
        .order_by(col(ReviewResult.id).desc())
    ).first()


def _latest_test_point_checkpoint(
    session: Session, task_id: int
) -> Optional[TaskTestPointCheckpoint]:
    return session.exec(
        select(TaskTestPointCheckpoint)
        .where(TaskTestPointCheckpoint.task_id == task_id)
        .order_by(col(TaskTestPointCheckpoint.attempt).desc())
    ).first()


def _citations_for_task(session: Session, task_id: int) -> list[TaskCitation]:
    return list(
        session.exec(
            select(TaskCitation)
            .where(TaskCitation.task_id == task_id)
            .order_by(col(TaskCitation.id).asc())
        ).all()
    )


def _reference_cases_for_task(session: Session, task_id: int) -> list[TaskReferenceCase]:
    return list(
        session.exec(
            select(TaskReferenceCase)
            .where(TaskReferenceCase.task_id == task_id)
            .order_by(col(TaskReferenceCase.id).asc())
        ).all()
    )


def _next_draft_version(session: Session, task_id: int) -> int:
    current = session.exec(
        select(func.max(CaseDraft.version)).where(CaseDraft.task_id == task_id)
    ).one()
    if current is None:
        return 1
    return int(current) + 1


def _fail_task(
    session: Session,
    task: GenerationTask,
    message: str,
    *,
    publish_stream: bool = False,
) -> GenerationTask:
    try:
        if task.status != "failed":
            _set_status(task, "failed")
    except InvalidTransition:
        task.status = "failed"
        task.updated_at = _utcnow()
    task.error_message = message
    session.add(task)
    append_event(session, task.id, "error", message)
    session.commit()
    session.refresh(task)
    if publish_stream:
        # Post-commit so the SSE terminal event agrees with the durable state.
        # Review/optimize failures use this helper without publish_stream so
        # they never overwrite a retained generation terminal preview.
        task_stream.fail(task.id, message=message)
    return task


def _resolve_chat_fn(
    chat_fn: Optional[ChatFn],
    stage_fn: Optional[ChatFn] = None,
    *,
    stage: Optional[str] = None,
) -> Optional[ChatFn]:
    """Resolve chat hook dynamically from explicit arg, stage hook, or facade attrs."""
    if chat_fn is not None:
        return chat_fn
    if stage_fn is not None:
        return stage_fn
    if stage is not None:
        attr = _STAGE_HOOK_ATTRS.get(stage)
        if attr:
            hook = _facade_hook(attr)
            if hook is not None:
                return hook
    pipeline_fn = _facade_hook("_PIPELINE_CHAT_FN")
    if pipeline_fn is not None:
        return pipeline_fn
    return _facade_hook("_GENERATE_CHAT_FN")


def _call_chat(
    chat_fn: Optional[ChatFn],
    *,
    model: ModelConfig,
    messages: list[dict[str, str]],
    stage_fn: Optional[ChatFn] = None,
    stream: bool = False,
    on_attempt: Optional[Callable[[int, bool], None]] = None,
    on_delta: Optional[Callable[[str], None]] = None,
    on_retry: Optional[Callable[[int, str], None]] = None,
) -> str:
    fn = _resolve_chat_fn(chat_fn, stage_fn)
    if fn is not None:
        if on_attempt is not None:
            on_attempt(1, False)
        try:
            result = fn(messages=messages, model=model)
        except TypeError:
            # Allow simpler signatures used in tests: fn(messages) or fn(**kwargs)
            try:
                result = fn(messages)
            except TypeError:
                result = fn(
                    base_url=model.base_url,
                    api_key=model.api_key,
                    model=model.model_name,
                    messages=messages,
                )
        content = str(result[0]) if isinstance(result, tuple) else str(result)
        if stream and on_delta is not None and content:
            on_delta(content)
        return content

    content, _usage = chat_completion(
        base_url=model.base_url,
        api_key=model.api_key,
        model=model.model_name,
        protocol=getattr(model, "protocol", "chat_completions") or "chat_completions",
        messages=messages,
        stream=stream,
        on_attempt=on_attempt,
        on_delta=on_delta,
        on_retry=on_retry,
    )
    return content


def invoke_chat(
    chat_fn: Optional[ChatFn],
    *,
    model: ModelConfig,
    messages: list[dict[str, str]],
    stage: Optional[str] = None,
    stage_fn: Optional[ChatFn] = None,
    stream: bool = False,
    on_attempt: Optional[Callable[[int, bool], None]] = None,
    on_delta: Optional[Callable[[str], None]] = None,
    on_retry: Optional[Callable[[int, str], None]] = None,
) -> str:
    """Invoke LLM chat while respecting monkeypatches on the task_pipeline facade."""
    resolved_stage_fn = stage_fn
    if resolved_stage_fn is None and stage is not None:
        attr = _STAGE_HOOK_ATTRS.get(stage)
        if attr:
            resolved_stage_fn = _facade_hook(attr)
    facade = _get_facade()
    facade_call = getattr(facade, "_call_chat", None) if facade is not None else None
    target = facade_call if callable(facade_call) else _call_chat
    return target(
        chat_fn,
        model=model,
        messages=messages,
        stage_fn=resolved_stage_fn,
        stream=stream,
        on_attempt=on_attempt,
        on_delta=on_delta,
        on_retry=on_retry,
    )
