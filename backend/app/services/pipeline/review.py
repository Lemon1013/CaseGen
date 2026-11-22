"""Case review, prompt optimization, task-scoped prompt application, and regeneration."""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlmodel import Session

from app import config
from app.models.entities import (
    CaseDraft,
    GenerationTask,
    PromptRevision,
    Requirement,
    ReviewResult,
    TaskCitation,
)
from app.services.pipeline.common import (
    ChatFn,
    LLMError,
    _citations_for_task,
    _fail_task,
    _focus_tags,
    _latest_draft,
    _latest_review,
    _resolve_default_generate_prompt,
    _resolve_generate_prompt,
    _resolve_model,
    _resolve_prompt_by_type,
    _resolve_review_model,
    _set_status,
    _utcnow,
    invoke_chat,
)
from app.services.pipeline.orchestrator import run_generate
from app.services.review_parse import parse_review_payload
from app.services.task_events import append_event
from app.services.task_state import InvalidTransition


def _build_review_messages(
    system_prompt: str,
    requirement: Requirement,
    draft: CaseDraft,
    citations: list[TaskCitation],
) -> list[dict[str, str]]:
    tags = _focus_tags(requirement)
    cite_lines: list[str] = []
    wiki_index = 0
    source_index = 0
    external_index = 0
    for citation in citations:
        if citation.citation_type == "source":
            source_index += 1
            label = f"[S{source_index}]"
            try:
                clauses = json.loads(citation.clause_ids_json or "[]")
            except json.JSONDecodeError:
                clauses = []
            metadata = [f"source_chunk_id={citation.source_chunk_id}"]
            if citation.anchor_clause:
                metadata.append(f"锚定条款={citation.anchor_clause}")
            elif isinstance(clauses, list) and clauses:
                metadata.append(f"含条款={','.join(str(item) for item in clauses[:8])}")
            excerpt = citation.content_excerpt or citation.snippet or ""
        elif citation.citation_type == "external":
            external_index += 1
            label = f"[E{external_index}]"
            metadata = []
            if getattr(citation, "heading_path", None):
                metadata.append(f"heading={citation.heading_path}")
            if getattr(citation, "knowledge_source_id", None) is not None:
                metadata.append(f"source_id={citation.knowledge_source_id}")
            excerpt = citation.content_excerpt or citation.snippet or ""
        else:
            wiki_index += 1
            label = f"[{wiki_index}]"
            metadata = [f"wiki_page_id={citation.wiki_page_id}"]
            excerpt = citation.snippet or citation.content_excerpt or ""
        meta_suffix = f" {' '.join(metadata)}" if metadata else ""
        cite_lines.append(
            f"{label} {citation.title} ({citation.path}) "
            f"score={citation.score}{meta_suffix}\n{excerpt}"
        )
    user_parts = [
        "# 需求",
        f"标题：{requirement.title}",
        f"描述：{requirement.description}",
    ]
    if tags:
        user_parts.append(f"关注标签：{', '.join(tags)}")
    user_parts.append("")
    user_parts.append("# 引用证据（外部知识 [E#] / Wiki [n] / 原文 [S#]，标签与生成阶段一致）")
    user_parts.append("\n\n".join(cite_lines) if cite_lines else "（无引用）")
    user_parts.append("")
    user_parts.append(f"# 用例草稿 v{draft.version}")
    user_parts.append(draft.content_md or "")
    user_parts.append("")
    user_parts.append(f"终版分数门槛：{config.FINAL_SCORE_THRESHOLD}")
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
        if task.status in ("generated", "failed", "reviewed"):
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

        content = invoke_chat(
            chat_fn,
            model=model,
            messages=messages,
            stage="review",
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
            active_gen = _resolve_default_generate_prompt(session)
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

        content = invoke_chat(
            chat_fn,
            model=model,
            messages=messages,
            stage="optimize",
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
    content: str | None = None,
) -> GenerationTask:
    if mode != "task_temp":
        raise ValueError("已禁止修改全局提示词，仅支持应用到当前任务（task_temp）")

    task = session.get(GenerationTask, task_id)
    if task is None:
        raise ValueError(f"GenerationTask id={task_id} not found")

    revision = session.get(PromptRevision, revision_id)
    if revision is None or revision.task_id != task.id:
        raise ValueError(f"PromptRevision id={revision_id} not found for task")
    if revision.status != "pending":
        raise ValueError("Only pending prompt revisions can be applied")

    edited = content is not None
    if content is not None:
        normalized_content = content.strip()
        if not normalized_content:
            raise ValueError("Prompt content must not be empty")
        revision.new_content = normalized_content
        session.add(revision)

    task.temp_prompt_content = revision.new_content
    session.add(task)
    revision.status = "applied_task_temp"
    session.add(revision)
    append_event(
        session,
        task.id,
        "apply_prompt",
        f"已应用 revision#{revision_id} 为任务临时提示词",
        detail={"mode": mode, "revision_id": revision_id, "edited": edited},
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
        return _fail_task(session, task, str(exc), publish_stream=True)

    return run_generate(session, task_id, chat_fn=chat_fn)
