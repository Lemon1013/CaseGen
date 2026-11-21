"""Unified point-by-point test-case generation stage with per-point lean fallback."""

from __future__ import annotations

from typing import Optional

from sqlmodel import Session

from app.models.entities import (
    CaseDraft,
    GenerationTask,
    Requirement,
)
from app.services.pipeline.common import (
    ChatFn,
    LLMError,
    _LEAN_GENERATE_SYSTEM,
    _generation_stopped,
    _latest_test_point_checkpoint,
    _next_draft_version,
    _resolve_generate_prompt,
    _resolve_model,
    _set_status,
    invoke_chat,
)
from app.services.pipeline.context import (
    _append_supplemental_context,
    _build_point_generate_messages,
    _citations_for_point,
    _link_draft_to_test_points,
    _normalize_point_chunk_case_keys,
    _reference_cases_text,
)
from app.services.task_events import append_event
from app.services.task_stream import task_stream
from app.services.test_points import current_points

# Context limits applied when a single test point fails with LLMError and retries
# in lean mode (reference cases are dropped and per-point evidence is capped).
_LEAN_POINT_CITATION_MAX_CHARS = 2400
_LEAN_SUPPLEMENTAL_MAX_CHARS = 1200


def run_point_by_point_generation(
    session: Session,
    task: GenerationTask,
    requirement: Requirement,
    *,
    stream_task_id: int,
    supplemental_text: str = "",
    confirmed_citation_ids: list[int] | None = None,
    chat_fn: Optional[ChatFn] = None,
) -> GenerationTask:
    """Generate complete cases strictly point-by-point from confirmed test points."""
    if task.status != "generating":
        _set_status(task, "generating")
        session.add(task)
        append_event(session, task.id, "generate", "开始调用 LLM 生成用例")
        session.commit()
        session.refresh(task)

    test_point_checkpoint = _latest_test_point_checkpoint(session, task.id)
    if test_point_checkpoint is None or test_point_checkpoint.status != "confirmed":
        raise ValueError("Cannot generate complete cases before test points are confirmed")

    confirmed_points = [
        p
        for p in current_points(session, task.id)
        if p.is_selected and not p.is_excluded
    ]
    if not confirmed_points:
        raise ValueError("Cannot generate complete cases without confirmed test points")

    reference_cases_text = _reference_cases_text(session, task.id)
    system_prompt, prompt_ref = _resolve_generate_prompt(session, task)
    model = _resolve_model(session, task)

    def publish_attempt(attempt: int, reset: bool) -> None:
        if reset:
            task_stream.reset(
                stream_task_id,
                status="generating",
                message=f"上游第 {attempt} 次尝试，已替换此前未完成输出",
            )
        else:
            task_stream.notice(
                stream_task_id,
                message=f"开始接收模型输出（第 {attempt} 次尝试）",
            )

    def publish_delta(delta: str) -> None:
        task_stream.delta(stream_task_id, delta)

    def publish_retry(next_attempt: int, message: str) -> None:
        task_stream.retry(
            stream_task_id,
            attempt=next_attempt,
            message=f"模型连接重试（第 {next_attempt} 次）：{message[:300]}",
        )

    total_points = len(confirmed_points)
    point_chunks: list[str] = []
    seen_case_keys: set[str] = set()
    granularity = getattr(task, "generation_granularity", "standard") or "standard"
    used_lean_fallback = False
    lean_fallback_points: list[str] = []

    for idx, pt in enumerate(confirmed_points, start=1):
        # Cooperative exit: a manual stop flips the durable status while this
        # loop is awaiting the LLM. Discard partial chunks and keep failed state.
        if _generation_stopped(session, task):
            point_chunks.clear()
            append_event(session, task.id, "generate", "检测到手动停止，中止后续生成")
            session.commit()
            return task

        progress_msg = f"正在生成测试用例 [{idx}/{total_points}]: {pt.stable_key} · {pt.title}"
        task_stream.status(
            stream_task_id,
            status="generating",
            message=progress_msg,
        )
        pt_citations = _citations_for_point(
            session,
            task.id,
            pt,
            fallback_citation_ids=confirmed_citation_ids,
        )
        pt_messages = _build_point_generate_messages(
            system_prompt,
            requirement,
            pt,
            idx,
            total_points,
            granularity=granularity,
            citations_text=pt_citations,
            reference_cases_text=reference_cases_text,
        )
        _append_supplemental_context(pt_messages, supplemental_text)

        try:
            chunk = invoke_chat(
                chat_fn,
                model=model,
                messages=pt_messages,
                stage="generate",
                stream=True,
                on_attempt=publish_attempt,
                on_delta=publish_delta,
                on_retry=publish_retry,
            )
        except LLMError as primary_exc:
            if _generation_stopped(session, task):
                point_chunks.clear()
                append_event(session, task.id, "generate", "检测到手动停止，中止后续生成")
                session.commit()
                return task
            # Per-point lean fallback: drop reference cases, cap point citation
            # length, and append lean system instructions so one oversized/flaky
            # point does not fail the entire batch.
            lean_citations = _citations_for_point(
                session,
                task.id,
                pt,
                fallback_citation_ids=confirmed_citation_ids,
                max_chars=_LEAN_POINT_CITATION_MAX_CHARS,
            )
            lean_system_prompt = (
                system_prompt.rstrip()
                + "\n\n# 精简重试补充\n"
                + _LEAN_GENERATE_SYSTEM
            )
            lean_messages = _build_point_generate_messages(
                lean_system_prompt,
                requirement,
                pt,
                idx,
                total_points,
                granularity=granularity,
                citations_text=lean_citations,
                reference_cases_text="",
            )
            _append_supplemental_context(
                lean_messages,
                (supplemental_text or "")[:_LEAN_SUPPLEMENTAL_MAX_CHARS],
            )
            append_event(
                session,
                task.id,
                "generate",
                f"测试点 {pt.stable_key} 主生成失败，精简上下文重试: {primary_exc}",
                detail={
                    "point_key": pt.stable_key,
                    "point_index": idx,
                    "total_points": total_points,
                    "lean_fallback": True,
                    "lean_citation_chars": len(lean_citations),
                    "dropped_reference_cases": bool(reference_cases_text.strip()),
                    "primary_error": str(primary_exc)[:300],
                },
            )
            session.commit()
            task_stream.notice(
                stream_task_id,
                message=f"测试点 {pt.stable_key} 主生成失败，正在使用精简上下文重试",
            )
            try:
                chunk = invoke_chat(
                    chat_fn,
                    model=model,
                    messages=lean_messages,
                    stage="generate",
                    stream=True,
                    on_attempt=publish_attempt,
                    on_delta=publish_delta,
                    on_retry=publish_retry,
                )
            except LLMError:
                if _generation_stopped(session, task):
                    point_chunks.clear()
                    append_event(session, task.id, "generate", "检测到手动停止，中止后续生成")
                    session.commit()
                    return task
                raise
            used_lean_fallback = True
            lean_fallback_points.append(pt.stable_key)

        stopped_after_call = _generation_stopped(session, task)
        if chunk and str(chunk).strip():
            normalized_chunk = _normalize_point_chunk_case_keys(
                str(chunk).strip(), pt, seen_case_keys
            )
            point_chunks.append(normalized_chunk)
            if idx < total_points and not stopped_after_call:
                task_stream.delta(stream_task_id, "\n\n")
            append_event(
                session,
                task.id,
                "generate",
                f"测试点用例生成完成 [{idx}/{total_points}] {pt.stable_key} · {pt.title}",
            )
            session.commit()

        if stopped_after_call:
            point_chunks.clear()
            append_event(session, task.id, "generate", "检测到手动停止，中止后续生成")
            session.commit()
            return task

    content = "\n\n".join(point_chunks)
    if not content or not str(content).strip():
        raise LLMError("Empty LLM content")

    # Final cooperative exit before persisting anything: stop may have flipped
    # the task to failed while the last LLM call was in flight.
    if _generation_stopped(session, task):
        append_event(session, task.id, "generate", "检测到手动停止，中止后续生成")
        session.commit()
        return task

    if used_lean_fallback:
        prompt_ref = f"{prompt_ref}|lean_fallback"

    version = _next_draft_version(session, task.id)
    draft = CaseDraft(
        task_id=task.id,
        version=version,
        content_md=str(content),
        prompt_version_ref=prompt_ref,
    )
    session.add(draft)
    session.flush()

    _link_draft_to_test_points(session, draft, confirmed_points)

    _set_status(task, "generated")
    task.error_message = None
    session.add(task)
    append_event(
        session,
        task.id,
        "generate",
        f"生成完成，draft v{version}"
        + ("（lean_fallback）" if used_lean_fallback else ""),
        detail={
            "draft_version": version,
            "model_id": model.id,
            "prompt_ref": prompt_ref,
            "lean_fallback": used_lean_fallback,
            "lean_fallback_points": lean_fallback_points,
        },
    )
    session.commit()
    session.refresh(task)
    task_stream.complete(
        stream_task_id,
        text=str(content),
        status="generated",
        message=f"生成完成，draft v{version}",
    )
    return task
