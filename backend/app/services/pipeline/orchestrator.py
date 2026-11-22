"""Top-level generation pipeline orchestration and task finalization."""

from __future__ import annotations

from typing import Optional

from sqlmodel import Session, col, select

from app.models.entities import (
    CaseDraft,
    GenerationTask,
    Requirement,
    TaskRetrievalCheckpoint,
    TestPoint,
)
from app.services.case_management import (
    import_cases_from_draft,
    split_case_draft,
    utcnow,
)
from app.services.pipeline.common import (
    ChatFn,
    LLMError,
    _fail_task,
    _latest_draft,
    _latest_test_point_checkpoint,
    _resolve_model,
    _set_status,
)
from app.services.pipeline.generation import run_point_by_point_generation
from app.services.pipeline.retrieval import (
    RetrievalStrategy,
    load_confirmed_retrieval_context,
    run_retrieval_stage,
)
from app.services.pipeline.test_points import run_generate_test_points
from app.services.task_events import append_event
from app.services.task_state import InvalidTransition
from app.services.task_stream import task_stream


def run_generate(
    session: Session,
    task_id: int,
    chat_fn: Optional[ChatFn] = None,
    auto_review: bool = False,
    retrieval_strategy: Optional[RetrievalStrategy] = None,
) -> GenerationTask:
    """Orchestrate retrieval checkpoint -> test-point checkpoint -> point-by-point generation."""
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise ValueError(f"GenerationTask id={task_id} not found")
    if task.status == "generating_test_points":
        return run_generate_test_points(session, task_id, chat_fn=chat_fn)

    stream_task_id = int(task.id or task_id)
    existing_stream = task_stream.snapshot(stream_task_id)
    if existing_stream is None or existing_stream.get("terminal") is not None:
        task_stream.start(
            stream_task_id,
            status=task.status,
            message="后台生成任务已启动",
        )

    # Compatibility barrier for tasks created before the durable test-point
    # checkpoint existed: never skip straight to complete-case generation.
    if task.status == "generating" and _latest_test_point_checkpoint(session, task.id) is None:
        legacy_retrieval = session.exec(
            select(TaskRetrievalCheckpoint)
            .where(TaskRetrievalCheckpoint.task_id == task.id)
            .order_by(col(TaskRetrievalCheckpoint.attempt).desc())
        ).first()
        if legacy_retrieval is not None and legacy_retrieval.status == "confirmed":
            _set_status(task, "generating_test_points")
            task.error_message = None
            session.add(task)
            append_event(
                session,
                task.id,
                "recovery",
                "兼容恢复：旧生成任务迁移到测试点生成阶段",
                detail={"retrieval_checkpoint_id": legacy_retrieval.id},
            )
            session.commit()
            session.refresh(task)
            task_stream.status(
                stream_task_id,
                status="generating_test_points",
                message="兼容恢复：正在生成测试点",
            )
            return run_generate_test_points(session, task_id, chat_fn=chat_fn)
        if legacy_retrieval is not None and legacy_retrieval.status == "pending":
            _set_status(task, "awaiting_confirmation")
            task.error_message = None
            session.add(task)
            append_event(
                session,
                task.id,
                "recovery",
                "兼容恢复：旧生成任务返回检索确认检查点",
                detail={"retrieval_checkpoint_id": legacy_retrieval.id},
            )
            session.commit()
            session.refresh(task)
            task_stream.status(
                stream_task_id,
                status="awaiting_confirmation",
                message="请确认检索引用后继续生成测试点",
            )
            return task
        _set_status(task, "retrieving")
        task.error_message = None
        session.add(task)
        append_event(session, task.id, "recovery", "兼容恢复：旧生成任务重新进入检索阶段")
        session.commit()
        session.refresh(task)

    requirement = session.get(Requirement, task.requirement_id)
    if requirement is None:
        return _fail_task(
            session,
            task,
            f"Requirement id={task.requirement_id} not found",
            publish_stream=True,
        )

    try:
        # Fail before changing state or touching the retriever when no usable
        # model configuration exists.
        try:
            _resolve_model(session, task)
        except Exception as exc:  # noqa: BLE001
            return _fail_task(session, task, str(exc), publish_stream=True)

        checkpoint = session.exec(
            select(TaskRetrievalCheckpoint)
            .where(TaskRetrievalCheckpoint.task_id == task.id)
            .where(TaskRetrievalCheckpoint.status == "confirmed")
            .order_by(col(TaskRetrievalCheckpoint.attempt).desc())
        ).first()
        resume_confirmed = checkpoint is not None and task.status == "generating"

        if task.status in ("draft", "failed", "regenerating"):
            _set_status(task, "retrieving")
            session.add(task)
            append_event(session, task.id, "retrieve", "开始检索外部知识库")
            session.commit()
            session.refresh(task)
        elif not resume_confirmed and task.status != "retrieving":
            raise InvalidTransition(
                f"Cannot start generate from status {task.status!r}"
            )

        if not resume_confirmed:
            return run_retrieval_stage(
                session,
                task,
                requirement,
                stream_task_id=stream_task_id,
                auto_review=auto_review,
                retrieval_strategy=retrieval_strategy,
            )

        assert checkpoint is not None
        _query, _context, supplemental_text, selected_citation_ids = (
            load_confirmed_retrieval_context(checkpoint)
        )
        return run_point_by_point_generation(
            session,
            task,
            requirement,
            stream_task_id=stream_task_id,
            supplemental_text=supplemental_text,
            confirmed_citation_ids=selected_citation_ids,
            chat_fn=chat_fn,
        )
    except InvalidTransition as exc:
        return _fail_task(session, task, str(exc), publish_stream=True)
    except LLMError as exc:
        return _fail_task(
            session,
            task,
            f"LLM error: {exc}",
            publish_stream=True,
        )
    except Exception as exc:  # noqa: BLE001 — surface any pipeline failure on the task
        return _fail_task(session, task, str(exc), publish_stream=True)


def finalize_task(
    session: Session,
    task_id: int,
    draft_id: int | None = None,
    *,
    overwrite_existing: bool = False,
    operator: str | None = None,
) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise ValueError(f"GenerationTask id={task_id} not found")

    draft = (
        session.get(CaseDraft, draft_id)
        if draft_id is not None
        else _latest_draft(session, task.id)
    )
    if draft is None:
        raise ValueError("Cannot finalize task without a case draft")
    if draft.task_id != task.id:
        raise ValueError("Selected draft does not belong to task")

    if task.status == "finalized":
        if task.finalized_draft_id not in (None, draft.id):
            raise ValueError("Task is already finalized with another draft")
        if task.finalized_draft_id == draft.id:
            return task
        import_cases_from_draft(
            session,
            task,
            draft,
            overwrite_existing=overwrite_existing,
            operator=operator,
        )
        task.finalized_draft_id = draft.id
        task.finalized_at = task.finalized_at or utcnow()
        session.add(task)
        session.commit()
        session.refresh(task)
        return task

    if task.status not in {"generated", "reviewed"}:
        raise ValueError(f"Cannot finalize task from status {task.status!r}")

    test_point_checkpoint = _latest_test_point_checkpoint(session, task.id)
    if test_point_checkpoint is not None:
        if test_point_checkpoint.status != "confirmed":
            raise ValueError("Cannot finalize before the test-point checkpoint is confirmed")
        current_keys = {
            row.stable_key.upper()
            for row in session.exec(
                select(TestPoint).where(TestPoint.checkpoint_id == test_point_checkpoint.id)
            ).all()
            if row.stable_key
        }
        sections = split_case_draft(draft.content_md)
        for section in sections:
            if not section.get("priority_present"):
                raise ValueError(
                    f"Case {section.get('case_key') or '?'} must include an explicit P0/P1/P2 priority"
                )
            point_keys = {str(item).upper() for item in section.get("test_point_keys") or []}
            if not point_keys:
                raise ValueError(
                    f"Case {section.get('case_key') or '?'} must include a current test-point key"
                )
            unknown_keys = sorted(point_keys - current_keys)
            if unknown_keys:
                raise ValueError(
                    f"Case {section.get('case_key') or '?'} references unknown test point(s): "
                    + ", ".join(unknown_keys)
                )

    imported = import_cases_from_draft(
        session,
        task,
        draft,
        overwrite_existing=overwrite_existing,
        operator=operator,
    )

    try:
        _set_status(task, "finalized")
    except InvalidTransition as exc:
        raise ValueError(str(exc)) from exc

    task.error_message = None
    task.finalized_draft_id = draft.id
    task.finalized_at = utcnow()
    session.add(task)
    append_event(
        session,
        task.id,
        "finalize",
        f"任务已终版，draft v{draft.version}",
        detail={
            "draft_id": draft.id,
            "draft_version": draft.version,
            "imported_case_ids": [int(row.id) for row in imported if row.id is not None],
            "imported_case_count": len(imported),
        },
    )
    session.commit()
    session.refresh(task)
    return task
