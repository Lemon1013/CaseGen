from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlmodel import Session, col, func, select

from app import config
from app.models.entities import (
    CaseDraft,
    GenerationTask,
    ModelConfig,
    PromptTemplate,
    Requirement,
    TaskCitation,
)
from app.services.llm import LLMError, chat_completion
from app.services.retrieve import load_all_wiki_pages, rank_pages
from app.services.task_events import append_event
from app.services.task_state import InvalidTransition, transition

# Optional injectable chat hook for tests: (messages, model_cfg) -> str
# Or set via run_generate(..., chat_fn=...).
_GENERATE_CHAT_FN: Optional[Callable[..., str]] = None

ChatFn = Callable[..., Any]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _set_status(task: GenerationTask, new_status: str) -> None:
    task.status = transition(task.status, new_status)
    task.updated_at = _utcnow()


def _focus_tags(requirement: Requirement) -> list[str]:
    try:
        tags = json.loads(requirement.focus_tags_json or "[]")
    except json.JSONDecodeError:
        tags = []
    if not isinstance(tags, list):
        return []
    return [str(t) for t in tags]


def _build_query(requirement: Requirement) -> str:
    tags = _focus_tags(requirement)
    parts = [requirement.title or "", requirement.description or ""]
    if tags:
        parts.append(" ".join(tags))
    return " ".join(p for p in parts if p).strip()


def _resolve_generate_prompt(session: Session, task: GenerationTask) -> tuple[str, str]:
    """Return (prompt_content, prompt_version_ref)."""
    if task.temp_prompt_content:
        return task.temp_prompt_content, "temp"

    if task.prompt_template_id is not None:
        row = session.get(PromptTemplate, task.prompt_template_id)
        if row is not None:
            return row.content, f"id:{row.id}:v{row.version}"

    active = session.exec(
        select(PromptTemplate).where(
            PromptTemplate.type == "generate",
            PromptTemplate.is_active == True,  # noqa: E712
        )
    ).first()
    if active is None:
        raise RuntimeError("No active generate prompt template found")
    return active.content, f"id:{active.id}:v{active.version}"


def _resolve_model(session: Session, task: GenerationTask) -> ModelConfig:
    if task.model_id is not None:
        row = session.get(ModelConfig, task.model_id)
        if row is None:
            raise RuntimeError(f"ModelConfig id={task.model_id} not found")
        return row
    default = session.exec(
        select(ModelConfig).where(ModelConfig.is_default == True)  # noqa: E712
    ).first()
    if default is None:
        # Fall back to any model if no default is marked.
        default = session.exec(select(ModelConfig).order_by(col(ModelConfig.id))).first()
    if default is None:
        raise RuntimeError("No ModelConfig available")
    return default


def _clear_citations(session: Session, task_id: int) -> None:
    rows = session.exec(select(TaskCitation).where(TaskCitation.task_id == task_id)).all()
    for row in rows:
        session.delete(row)
    session.flush()


def _truncate_wiki_context(pages: list[dict[str, Any]], max_chars: int) -> str:
    blocks: list[str] = []
    used = 0
    for i, page in enumerate(pages, start=1):
        title = page.get("title") or ""
        path = page.get("path") or ""
        content = page.get("content") or ""
        header = f"[{i}] {title} ({path})\n"
        remaining = max_chars - used - len(header)
        if remaining <= 0:
            break
        body = content if len(content) <= remaining else content[:remaining]
        block = header + body
        blocks.append(block)
        used += len(block) + 2  # account for separator
        if used >= max_chars:
            break
    return "\n\n".join(blocks)


def _build_messages(
    system_prompt: str,
    requirement: Requirement,
    wiki_context: str,
) -> list[dict[str, str]]:
    tags = _focus_tags(requirement)
    user_parts = [
        "# 需求",
        f"标题：{requirement.title}",
        f"描述：{requirement.description}",
    ]
    if tags:
        user_parts.append(f"关注标签：{', '.join(tags)}")
    user_parts.append("")
    user_parts.append("# Wiki 引用上下文")
    user_parts.append(wiki_context if wiki_context.strip() else "（无匹配 Wiki 页面）")
    user_parts.append("")
    user_parts.append("请根据以上需求与 Wiki 上下文生成测试用例 Markdown。")
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def _call_chat(
    chat_fn: Optional[ChatFn],
    *,
    model: ModelConfig,
    messages: list[dict[str, str]],
) -> str:
    fn = chat_fn if chat_fn is not None else _GENERATE_CHAT_FN
    if fn is not None:
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
        if isinstance(result, tuple):
            return str(result[0])
        return str(result)

    content, _usage = chat_completion(
        base_url=model.base_url,
        api_key=model.api_key,
        model=model.model_name,
        messages=messages,
    )
    return content


def _next_draft_version(session: Session, task_id: int) -> int:
    current = session.exec(
        select(func.max(CaseDraft.version)).where(CaseDraft.task_id == task_id)
    ).one()
    if current is None:
        return 1
    return int(current) + 1


def _fail_task(session: Session, task: GenerationTask, message: str) -> GenerationTask:
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
    return task


def run_generate(
    session: Session,
    task_id: int,
    chat_fn: Optional[ChatFn] = None,
) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise ValueError(f"GenerationTask id={task_id} not found")

    requirement = session.get(Requirement, task.requirement_id)
    if requirement is None:
        return _fail_task(session, task, f"Requirement id={task.requirement_id} not found")

    try:
        # Always re-retrieve: draft/failed/regenerating → retrieving
        if task.status in ("draft", "failed", "regenerating"):
            _set_status(task, "retrieving")
            session.add(task)
            append_event(session, task.id, "retrieve", "开始检索 Wiki 知识")
            session.commit()
            session.refresh(task)
        elif task.status != "retrieving":
            raise InvalidTransition(
                f"Cannot start generate from status {task.status!r}"
            )

        query = _build_query(requirement)
        pages = load_all_wiki_pages(session)
        hits = rank_pages(query, pages, top_k=config.RETRIEVE_TOP_K)

        _clear_citations(session, task.id)
        for hit in hits:
            session.add(
                TaskCitation(
                    task_id=task.id,
                    wiki_page_id=hit.get("id"),
                    title=hit.get("title") or "",
                    path=hit.get("path") or "",
                    score=float(hit.get("score") or 0.0),
                    snippet=hit.get("snippet") or "",
                )
            )
        append_event(
            session,
            task.id,
            "retrieve",
            f"检索完成，命中 {len(hits)} 条",
            detail={"query": query, "hit_count": len(hits)},
        )
        if not hits:
            append_event(
                session,
                task.id,
                "retrieve",
                "警告：未检索到相关 Wiki 页面，将仅基于需求生成",
            )
        session.commit()
        session.refresh(task)

        _set_status(task, "generating")
        session.add(task)
        append_event(session, task.id, "generate", "开始调用 LLM 生成用例")
        session.commit()
        session.refresh(task)

        system_prompt, prompt_ref = _resolve_generate_prompt(session, task)
        model = _resolve_model(session, task)
        wiki_context = _truncate_wiki_context(hits, config.MAX_WIKI_CONTEXT_CHARS)
        messages = _build_messages(system_prompt, requirement, wiki_context)

        content = _call_chat(chat_fn, model=model, messages=messages)
        if not content or not str(content).strip():
            raise LLMError("Empty LLM content")

        version = _next_draft_version(session, task.id)
        draft = CaseDraft(
            task_id=task.id,
            version=version,
            content_md=str(content),
            prompt_version_ref=prompt_ref,
        )
        session.add(draft)

        _set_status(task, "generated")
        task.error_message = None
        session.add(task)
        append_event(
            session,
            task.id,
            "generate",
            f"生成完成，draft v{version}",
            detail={"draft_version": version, "model_id": model.id, "prompt_ref": prompt_ref},
        )
        session.commit()
        session.refresh(task)
        return task

    except InvalidTransition as exc:
        return _fail_task(session, task, str(exc))
    except LLMError as exc:
        return _fail_task(session, task, f"LLM error: {exc}")
    except Exception as exc:  # noqa: BLE001 — surface any pipeline failure on the task
        return _fail_task(session, task, str(exc))
