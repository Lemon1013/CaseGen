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
    PromptRevision,
    PromptTemplate,
    Requirement,
    ReviewResult,
    TaskCitation,
)
from app.services.llm import LLMError, chat_completion
from app.services.retrieve import load_all_wiki_pages, rank_pages
from app.services.review_parse import parse_review_payload
from app.services.task_events import append_event
from app.services.task_state import InvalidTransition, transition

# Optional injectable chat hooks for tests: (messages, model_cfg) -> str
# Prefer explicit chat_fn arg, then stage-specific hook, then shared pipeline hook.
_PIPELINE_CHAT_FN: Optional[Callable[..., str]] = None
_GENERATE_CHAT_FN: Optional[Callable[..., str]] = None
_REVIEW_CHAT_FN: Optional[Callable[..., str]] = None
_OPTIMIZE_CHAT_FN: Optional[Callable[..., str]] = None

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


def _resolve_chat_fn(
    chat_fn: Optional[ChatFn],
    stage_fn: Optional[ChatFn] = None,
) -> Optional[ChatFn]:
    if chat_fn is not None:
        return chat_fn
    if stage_fn is not None:
        return stage_fn
    if _PIPELINE_CHAT_FN is not None:
        return _PIPELINE_CHAT_FN
    return _GENERATE_CHAT_FN


def _call_chat(
    chat_fn: Optional[ChatFn],
    *,
    model: ModelConfig,
    messages: list[dict[str, str]],
    stage_fn: Optional[ChatFn] = None,
) -> str:
    fn = _resolve_chat_fn(chat_fn, stage_fn)
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


def _resolve_prompt_by_type(session: Session, prompt_type: str) -> PromptTemplate:
    active = session.exec(
        select(PromptTemplate).where(
            PromptTemplate.type == prompt_type,
            PromptTemplate.is_active == True,  # noqa: E712
        )
    ).first()
    if active is None:
        raise RuntimeError(f"No active {prompt_type} prompt template found")
    return active


def _resolve_review_model(session: Session, task: GenerationTask) -> ModelConfig:
    if task.review_model_id is not None:
        row = session.get(ModelConfig, task.review_model_id)
        if row is None:
            raise RuntimeError(f"ModelConfig id={task.review_model_id} not found")
        return row
    return _resolve_model(session, task)


def _citations_for_task(session: Session, task_id: int) -> list[TaskCitation]:
    return list(
        session.exec(
            select(TaskCitation)
            .where(TaskCitation.task_id == task_id)
            .order_by(col(TaskCitation.id).asc())
        ).all()
    )


def _build_review_messages(
    system_prompt: str,
    requirement: Requirement,
    draft: CaseDraft,
    citations: list[TaskCitation],
) -> list[dict[str, str]]:
    tags = _focus_tags(requirement)
    cite_lines: list[str] = []
    for i, c in enumerate(citations, start=1):
        cite_lines.append(
            f"[{i}] {c.title} ({c.path}) score={c.score}\n{c.snippet or ''}"
        )
    user_parts = [
        "# 需求",
        f"标题：{requirement.title}",
        f"描述：{requirement.description}",
    ]
    if tags:
        user_parts.append(f"关注标签：{', '.join(tags)}")
    user_parts.append("")
    user_parts.append("# Wiki 引用")
    user_parts.append("\n\n".join(cite_lines) if cite_lines else "（无引用）")
    user_parts.append("")
    user_parts.append(f"# 用例草稿 v{draft.version}")
    user_parts.append(draft.content_md or "")
    user_parts.append("")
    user_parts.append("请按系统要求输出评审 JSON。")
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def _build_optimize_messages(
    system_prompt: str,
    current_generate_prompt: str,
    requirement: Requirement,
    review_payload: dict[str, Any],
) -> list[dict[str, str]]:
    tags = _focus_tags(requirement)
    user_parts = [
        "# 当前 generate 提示词",
        current_generate_prompt,
        "",
        "# 需求摘要",
        f"标题：{requirement.title}",
        f"描述：{requirement.description}",
    ]
    if tags:
        user_parts.append(f"关注标签：{', '.join(tags)}")
    user_parts.append("")
    user_parts.append("# 评审结果")
    user_parts.append(json.dumps(review_payload, ensure_ascii=False, indent=2))
    user_parts.append("")
    user_parts.append("请输出优化后的完整 generate 提示词正文。")
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def _deactivate_generate_siblings(
    session: Session, keep_id: Optional[int] = None
) -> None:
    rows = session.exec(
        select(PromptTemplate).where(
            PromptTemplate.type == "generate",
            PromptTemplate.is_active == True,  # noqa: E712
        )
    ).all()
    for row in rows:
        if keep_id is not None and row.id == keep_id:
            continue
        row.is_active = False
        row.updated_at = _utcnow()
        session.add(row)


def _next_prompt_version(session: Session, prompt_type: str) -> int:
    current = session.exec(
        select(func.max(PromptTemplate.version)).where(PromptTemplate.type == prompt_type)
    ).one()
    if current is None:
        return 1
    return int(current) + 1


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

        content = _call_chat(
            chat_fn,
            model=model,
            messages=messages,
            stage_fn=_GENERATE_CHAT_FN,
        )
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


def run_review(
    session: Session,
    task_id: int,
    chat_fn: Optional[ChatFn] = None,
) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise ValueError(f"GenerationTask id={task_id} not found")

    draft = _latest_draft(session, task.id)
    if draft is None:
        return _fail_task(session, task, "No case draft available for review")

    requirement = session.get(Requirement, task.requirement_id)
    if requirement is None:
        return _fail_task(session, task, f"Requirement id={task.requirement_id} not found")

    try:
        if task.status in ("generated", "failed"):
            _set_status(task, "reviewing")
            session.add(task)
            append_event(session, task.id, "review", "开始评审用例")
            session.commit()
            session.refresh(task)
        elif task.status != "reviewing":
            raise InvalidTransition(f"Cannot start review from status {task.status!r}")

        review_prompt = _resolve_prompt_by_type(session, "review")
        model = _resolve_review_model(session, task)
        citations = _citations_for_task(session, task.id)
        messages = _build_review_messages(
            review_prompt.content, requirement, draft, citations
        )

        content = _call_chat(
            chat_fn,
            model=model,
            messages=messages,
            stage_fn=_REVIEW_CHAT_FN,
        )
        payload = parse_review_payload(str(content) if content is not None else "")
        score = int(payload.get("score") or 0)
        verdict = str(payload.get("verdict") or "unknown")

        result = ReviewResult(
            task_id=task.id,
            draft_id=draft.id,
            score=score,
            verdict=verdict,
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        session.add(result)

        _set_status(task, "reviewed")
        task.error_message = None
        session.add(task)
        append_event(
            session,
            task.id,
            "review",
            f"评审完成 score={score} verdict={verdict}",
            detail={
                "score": score,
                "verdict": verdict,
                "draft_id": draft.id,
                "ready_for_final": payload.get("ready_for_final"),
            },
        )
        session.commit()
        session.refresh(task)
        return task

    except InvalidTransition as exc:
        return _fail_task(session, task, str(exc))
    except LLMError as exc:
        return _fail_task(session, task, f"LLM error: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _fail_task(session, task, str(exc))


def run_optimize_prompt(
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

    review = _latest_review(session, task.id)
    if review is None:
        return _fail_task(session, task, "No review result available for optimize")

    try:
        review_payload = json.loads(review.payload_json or "{}")
    except json.JSONDecodeError:
        review_payload = parse_review_payload(review.payload_json or "")

    try:
        if task.status in ("reviewed", "failed"):
            _set_status(task, "optimizing")
            session.add(task)
            append_event(session, task.id, "optimize", "开始优化 generate 提示词")
            session.commit()
            session.refresh(task)
        elif task.status != "optimizing":
            raise InvalidTransition(
                f"Cannot start optimize from status {task.status!r}"
            )

        generate_content, _ref = _resolve_generate_prompt(session, task)
        base_prompt_id: Optional[int] = None
        if task.prompt_template_id is not None:
            base_prompt_id = task.prompt_template_id
        else:
            active_gen = session.exec(
                select(PromptTemplate).where(
                    PromptTemplate.type == "generate",
                    PromptTemplate.is_active == True,  # noqa: E712
                )
            ).first()
            if active_gen is not None:
                base_prompt_id = active_gen.id

        optimize_prompt = _resolve_prompt_by_type(session, "optimize")
        model = _resolve_model(session, task)
        messages = _build_optimize_messages(
            optimize_prompt.content,
            generate_content,
            requirement,
            review_payload if isinstance(review_payload, dict) else {},
        )

        content = _call_chat(
            chat_fn,
            model=model,
            messages=messages,
            stage_fn=_OPTIMIZE_CHAT_FN,
        )
        new_content = str(content or "").strip()
        if not new_content:
            raise LLMError("Empty optimized prompt content")

        revision = PromptRevision(
            task_id=task.id,
            base_prompt_id=base_prompt_id,
            new_content=new_content,
            status="pending",
        )
        session.add(revision)

        _set_status(task, "reviewed")
        task.error_message = None
        session.add(task)
        append_event(
            session,
            task.id,
            "optimize",
            "提示词优化完成，revision pending",
            detail={"base_prompt_id": base_prompt_id},
        )
        session.commit()
        session.refresh(task)
        return task

    except InvalidTransition as exc:
        return _fail_task(session, task, str(exc))
    except LLMError as exc:
        return _fail_task(session, task, f"LLM error: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _fail_task(session, task, str(exc))


def apply_prompt(
    session: Session,
    task_id: int,
    revision_id: int,
    mode: str,
) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise ValueError(f"GenerationTask id={task_id} not found")

    revision = session.get(PromptRevision, revision_id)
    if revision is None or revision.task_id != task.id:
        raise ValueError(f"PromptRevision id={revision_id} not found for task")

    if mode not in ("global", "task_temp"):
        raise ValueError(f"Invalid apply mode {mode!r}")

    if mode == "task_temp":
        task.temp_prompt_content = revision.new_content
        session.add(task)
        revision.status = "applied_task_temp"
        session.add(revision)
        append_event(
            session,
            task.id,
            "apply_prompt",
            f"已应用 revision#{revision_id} 为任务临时提示词",
            detail={"mode": mode, "revision_id": revision_id},
        )
    else:
        # global: new active generate PromptTemplate version
        base_name = "generate"
        if revision.base_prompt_id is not None:
            base = session.get(PromptTemplate, revision.base_prompt_id)
            if base is not None:
                base_name = base.name
        _deactivate_generate_siblings(session)
        version = _next_prompt_version(session, "generate")
        row = PromptTemplate(
            name=base_name,
            type="generate",
            content=revision.new_content,
            version=version,
            is_active=True,
        )
        session.add(row)
        session.flush()
        task.prompt_template_id = row.id
        task.temp_prompt_content = None
        session.add(task)
        revision.status = "applied_global"
        session.add(revision)
        append_event(
            session,
            task.id,
            "apply_prompt",
            f"已应用 revision#{revision_id} 为全局 generate v{version}",
            detail={
                "mode": mode,
                "revision_id": revision_id,
                "prompt_template_id": row.id,
                "version": version,
            },
        )

    task.updated_at = _utcnow()
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def run_regenerate(
    session: Session,
    task_id: int,
    chat_fn: Optional[ChatFn] = None,
) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise ValueError(f"GenerationTask id={task_id} not found")

    try:
        if task.status in ("generated", "reviewed", "failed"):
            _set_status(task, "regenerating")
            session.add(task)
            append_event(session, task.id, "regenerate", "开始重新生成")
            session.commit()
            session.refresh(task)
        elif task.status != "regenerating":
            raise InvalidTransition(
                f"Cannot regenerate from status {task.status!r}"
            )
    except InvalidTransition as exc:
        return _fail_task(session, task, str(exc))

    return run_generate(session, task_id, chat_fn=chat_fn)


def finalize_task(session: Session, task_id: int) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise ValueError(f"GenerationTask id={task_id} not found")

    draft = _latest_draft(session, task.id)
    if draft is None:
        raise ValueError("Cannot finalize task without a case draft")

    try:
        _set_status(task, "finalized")
    except InvalidTransition as exc:
        raise ValueError(str(exc)) from exc

    task.error_message = None
    session.add(task)
    append_event(
        session,
        task.id,
        "finalize",
        f"任务已终版，draft v{draft.version}",
        detail={"draft_id": draft.id, "draft_version": draft.version},
    )
    session.commit()
    session.refresh(task)
    return task
