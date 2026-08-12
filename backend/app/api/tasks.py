from __future__ import annotations

import json
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlmodel import Session, col, select

from app.db import get_session
from app.models.entities import (
    CaseDraft,
    GenerationTask,
    ModelConfig,
    PromptRevision,
    PromptTemplate,
    Requirement,
    ReviewResult,
    SourceChunk,
    TaskCitation,
    TaskEvent,
    TestCase,
    WikiPageRow,
    WikiSpace,
)
from app.schemas.tasks import (
    ApplyPromptBody,
    CaseDraftOut,
    FinalizeTaskBody,
    PromptRevisionOut,
    ReviewResultOut,
    TaskCitationOut,
    TaskCreate,
    TaskEventOut,
    TaskModelUpdate,
    TaskOut,
)
from app.services.task_events import append_event
from app.services.task_jobs import (
    job_generate,
    job_optimize_prompt,
    job_regenerate,
    job_review,
)
from app.services.task_pipeline import (
    apply_prompt,
    finalize_task,
    run_generate,
    run_optimize_prompt,
    run_regenerate,
    run_review,
)
from app.services.task_state import InvalidTransition, transition
from app.services.wiki_spaces import resolve_space, resolve_space_id

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# Optional injectable chat hooks for tests: monkeypatch these module attrs.
_PIPELINE_CHAT_FN = None
_GENERATE_CHAT_FN = None
_REVIEW_CHAT_FN = None
_OPTIMIZE_CHAT_FN = None

# Statuses that mean a long-running job is already in flight
_BUSY = frozenset(
    {"retrieving", "generating", "reviewing", "optimizing", "regenerating"}
)


def _chat_for(stage: str = "generate"):
    """Resolve chat hook for a pipeline stage."""
    if stage == "review" and _REVIEW_CHAT_FN is not None:
        return _REVIEW_CHAT_FN
    if stage == "optimize" and _OPTIMIZE_CHAT_FN is not None:
        return _OPTIMIZE_CHAT_FN
    if stage == "generate" and _GENERATE_CHAT_FN is not None:
        return _GENERATE_CHAT_FN
    if _PIPELINE_CHAT_FN is not None:
        return _PIPELINE_CHAT_FN
    return _GENERATE_CHAT_FN


def _force_sync(stage: str = "generate", wait: bool = False) -> bool:
    """Sync when tests inject chat hooks, or client explicitly asks wait=1."""
    if wait:
        return True
    return _chat_for(stage) is not None


def _begin_status(
    session: Session,
    task: GenerationTask,
    new_status: str,
    step: str,
    message: str,
) -> GenerationTask:
    """Transition to an in-progress status so UI can poll immediately."""
    try:
        task.status = transition(task.status, new_status)
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    task.error_message = None
    session.add(task)
    append_event(session, task.id, step, message)
    session.commit()
    session.refresh(task)
    return task


def _tags_list(requirement: Optional[Requirement]) -> list[str]:
    if requirement is None:
        return []
    try:
        tags = json.loads(requirement.focus_tags_json or "[]")
    except json.JSONDecodeError:
        tags = []
    if not isinstance(tags, list):
        return []
    return [str(t) for t in tags]


def _latest_draft(session: Session, task_id: int) -> Optional[CaseDraft]:
    return session.exec(
        select(CaseDraft)
        .where(CaseDraft.task_id == task_id)
        .order_by(col(CaseDraft.version).desc())
    ).first()


def _latest_review_row(session: Session, task_id: int) -> Optional[ReviewResult]:
    return session.exec(
        select(ReviewResult)
        .where(ReviewResult.task_id == task_id)
        .order_by(col(ReviewResult.id).desc())
    ).first()


def _citation_count(session: Session, task_id: int) -> int:
    rows = session.exec(select(TaskCitation).where(TaskCitation.task_id == task_id)).all()
    return len(rows)


def _review_to_out(row: ReviewResult) -> ReviewResultOut:
    try:
        payload = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {"raw": row.payload_json}
    if not isinstance(payload, dict):
        payload = {"raw": row.payload_json}
    return ReviewResultOut(
        id=row.id,
        task_id=row.task_id,
        draft_id=row.draft_id,
        score=row.score,
        verdict=row.verdict,
        payload=payload,
        created_at=row.created_at,
    )


def to_task_out(session: Session, task: GenerationTask) -> TaskOut:
    requirement = session.get(Requirement, task.requirement_id)
    draft = _latest_draft(session, task.id)
    snippet = None
    version = None
    if draft is not None:
        version = draft.version
        text = draft.content_md or ""
        snippet = text[:240]

    review_row = _latest_review_row(session, task.id)
    latest_review = _review_to_out(review_row) if review_row is not None else None
    imported_case_rows = session.exec(
        select(TestCase)
        .where(
            TestCase.source_task_id == task.id,
            TestCase.source_draft_id == task.finalized_draft_id,
        )
        .order_by(col(TestCase.case_key).asc(), col(TestCase.id).asc())
    ).all() if task.finalized_draft_id is not None else []
    try:
        space = resolve_space(session, task.wiki_space_id)
    except ValueError:
        space = None

    return TaskOut(
        id=task.id,
        requirement_id=task.requirement_id,
        wiki_space_id=int(space.id if space and space.id is not None else task.wiki_space_id or 0),
        wiki_space_name=space.name if space else "",
        status=task.status,
        model_id=task.model_id,
        prompt_template_id=task.prompt_template_id,
        error_message=task.error_message,
        title=requirement.title if requirement else None,
        description=requirement.description if requirement else None,
        focus_tags=_tags_list(requirement),
        citation_count=_citation_count(session, task.id),
        latest_draft_snippet=snippet,
        latest_draft_version=version,
        latest_review=latest_review,
        finalized_draft_id=task.finalized_draft_id,
        finalized_at=task.finalized_at,
        imported_case_ids=[int(row.id) for row in imported_case_rows if row.id is not None],
        imported_case_count=len(imported_case_rows),
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.post("", response_model=TaskOut)
def create_task(
    body: TaskCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    wait: bool = Query(
        False,
        description="If true (or chat hooks injected), run generate inline before responding",
    ),
) -> TaskOut:
    try:
        space = resolve_space(session, body.wiki_space_id, for_write=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.prompt_template_id is not None:
        prompt = session.get(PromptTemplate, body.prompt_template_id)
        if prompt is None:
            raise HTTPException(status_code=422, detail="Prompt template not found")
        if prompt.type != "generate":
            raise HTTPException(
                status_code=422,
                detail="Only generate prompt templates can be selected",
            )
        if not prompt.is_active:
            raise HTTPException(
                status_code=422,
                detail="Only active generate prompt templates can be selected",
            )

    if body.requirement_id is not None:
        requirement = session.get(Requirement, body.requirement_id)
        if requirement is None:
            raise HTTPException(status_code=422, detail="Requirement not found")
    else:
        if not (body.title or "").strip() or not (body.description or "").strip():
            raise HTTPException(
                status_code=422,
                detail="title and description are required when requirement_id is omitted",
            )
        requirement = Requirement(
            title=(body.title or "").strip(),
            description=(body.description or "").strip(),
            focus_tags_json=json.dumps(body.focus_tags or [], ensure_ascii=False),
        )
        session.add(requirement)
        session.commit()
        session.refresh(requirement)

    task = GenerationTask(
        requirement_id=requirement.id,
        wiki_space_id=space.id,
        status="draft",
        model_id=body.model_id,
        prompt_template_id=body.prompt_template_id,
    )
    session.add(task)
    session.commit()
    session.refresh(task)

    if body.run_generate:
        if _force_sync("generate", wait):
            run_generate(session, task.id, chat_fn=_chat_for("generate"))
            session.refresh(task)
            if body.auto_review and task.status == "generated":
                run_review(session, task.id, chat_fn=_chat_for("review"))
                session.refresh(task)
        else:
            task = _begin_status(
                session, task, "retrieving", "retrieve", "任务已创建，后台开始检索/生成"
            )
            background_tasks.add_task(
                job_generate, task.id, bool(body.auto_review)
            )

    return to_task_out(session, task)


@router.get("", response_model=List[TaskOut])
def list_tasks(session: Session = Depends(get_session)) -> list[TaskOut]:
    rows = session.exec(select(GenerationTask).order_by(col(GenerationTask.id).desc())).all()
    return [to_task_out(session, r) for r in rows]


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: int, session: Session = Depends(get_session)) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return to_task_out(session, task)


@router.patch("/{task_id}/model", response_model=TaskOut)
def update_task_model(
    task_id: int,
    body: TaskModelUpdate,
    session: Session = Depends(get_session),
) -> TaskOut:
    """Change the model before an initial or failed generation attempt."""

    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in {"draft", "failed"}:
        raise HTTPException(
            status_code=409,
            detail="Model can only be changed for draft or failed tasks",
        )
    if body.model_id is not None and session.get(ModelConfig, body.model_id) is None:
        raise HTTPException(status_code=422, detail="Model not found")

    previous_model_id = task.model_id
    task.model_id = body.model_id
    session.add(task)
    append_event(
        session,
        task.id,
        "model_change",
        "已切换任务模型，下一次生成将使用新配置",
        detail={
            "previous_model_id": previous_model_id,
            "model_id": body.model_id,
        },
    )
    session.commit()
    session.refresh(task)
    return to_task_out(session, task)


@router.delete("/{task_id}")
def delete_task(task_id: int, session: Session = Depends(get_session)) -> dict:
    """Hard-delete a task and its dependent rows (drafts, reviews, events, …)."""
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    requirement_id = task.requirement_id

    for model in (TaskCitation, CaseDraft, ReviewResult, PromptRevision, TaskEvent):
        rows = session.exec(select(model).where(model.task_id == task_id)).all()
        for row in rows:
            session.delete(row)

    session.delete(task)
    session.flush()

    # Requirement is created 1:1 with the task; drop it if nothing else references it.
    other = session.exec(
        select(GenerationTask).where(GenerationTask.requirement_id == requirement_id)
    ).first()
    has_cases = session.exec(
        select(TestCase.id).where(TestCase.requirement_id == requirement_id)
    ).first()
    if other is None and has_cases is None:
        req = session.get(Requirement, requirement_id)
        if req is not None:
            session.delete(req)

    session.commit()
    return {"ok": True, "id": task_id}


@router.post("/{task_id}/generate", response_model=TaskOut)
def generate_task(
    task_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    wait: bool = Query(False),
    auto_review: bool = Query(False),
) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status in _BUSY:
        # Already running — do not double-schedule
        return to_task_out(session, task)

    if _force_sync("generate", wait):
        task = run_generate(session, task_id, chat_fn=_chat_for("generate"))
        if auto_review and task.status == "generated":
            task = run_review(session, task_id, chat_fn=_chat_for("review"))
        return to_task_out(session, task)

    if task.status not in ("draft", "failed", "regenerating"):
        # Preserve envelope: pipeline marks failed + error_message
        task = run_generate(session, task_id, chat_fn=_chat_for("generate"))
        return to_task_out(session, task)

    task = _begin_status(
        session, task, "retrieving", "retrieve", "后台开始检索 Wiki / 原文"
    )
    background_tasks.add_task(job_generate, task_id, auto_review)
    return to_task_out(session, task)


@router.post("/{task_id}/review", response_model=TaskOut)
def review_task(
    task_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    wait: bool = Query(False),
) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status in _BUSY:
        return to_task_out(session, task)

    if _force_sync("review", wait):
        task = run_review(session, task_id, chat_fn=_chat_for("review"))
        return to_task_out(session, task)

    if task.status not in ("generated", "failed"):
        task = run_review(session, task_id, chat_fn=_chat_for("review"))
        return to_task_out(session, task)

    task = _begin_status(session, task, "reviewing", "review", "后台开始评审")
    background_tasks.add_task(job_review, task_id)
    return to_task_out(session, task)


@router.post("/{task_id}/optimize-prompt", response_model=TaskOut)
def optimize_prompt_task(
    task_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    wait: bool = Query(False),
) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status in _BUSY:
        return to_task_out(session, task)

    if _force_sync("optimize", wait):
        task = run_optimize_prompt(session, task_id, chat_fn=_chat_for("optimize"))
        return to_task_out(session, task)

    if task.status not in ("reviewed", "failed"):
        task = run_optimize_prompt(session, task_id, chat_fn=_chat_for("optimize"))
        return to_task_out(session, task)

    task = _begin_status(
        session, task, "optimizing", "optimize", "后台开始优化 Prompt"
    )
    background_tasks.add_task(job_optimize_prompt, task_id)
    return to_task_out(session, task)


@router.post("/{task_id}/apply-prompt", response_model=TaskOut)
def apply_prompt_task(
    task_id: int,
    body: ApplyPromptBody,
    session: Session = Depends(get_session),
) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        task = apply_prompt(
            session,
            task_id,
            revision_id=body.revision_id,
            mode=body.mode,
            content=body.content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return to_task_out(session, task)


@router.post("/{task_id}/regenerate", response_model=TaskOut)
def regenerate_task(
    task_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    wait: bool = Query(False),
) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status in _BUSY:
        return to_task_out(session, task)

    if _force_sync("generate", wait):
        task = run_regenerate(session, task_id, chat_fn=_chat_for("generate"))
        return to_task_out(session, task)

    if task.status not in ("generated", "reviewed", "failed"):
        task = run_regenerate(session, task_id, chat_fn=_chat_for("generate"))
        return to_task_out(session, task)

    task = _begin_status(
        session, task, "regenerating", "regenerate", "后台开始重新生成"
    )
    background_tasks.add_task(job_regenerate, task_id)
    return to_task_out(session, task)


@router.post("/{task_id}/finalize", response_model=TaskOut)
def finalize_task_route(
    task_id: int,
    body: FinalizeTaskBody | None = None,
    session: Session = Depends(get_session),
) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        task = finalize_task(session, task_id, draft_id=body.draft_id if body else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return to_task_out(session, task)


@router.get("/{task_id}/drafts", response_model=List[CaseDraftOut])
def list_drafts(task_id: int, session: Session = Depends(get_session)) -> list[CaseDraft]:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    rows = session.exec(
        select(CaseDraft)
        .where(CaseDraft.task_id == task_id)
        .order_by(col(CaseDraft.version).desc())
    ).all()
    return list(rows)


@router.get("/{task_id}/citations", response_model=List[TaskCitationOut])
def list_citations(
    task_id: int, session: Session = Depends(get_session)
) -> list[TaskCitationOut]:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        task_space_id = int(resolve_space(session, task.wiki_space_id).id or 0)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    default_space_id = resolve_space_id(session)
    rows = session.exec(
        select(TaskCitation)
        .where(TaskCitation.task_id == task_id)
        .order_by(col(TaskCitation.id).asc())
    ).all()
    out: list[TaskCitationOut] = []
    for r in rows:
        cids: list[str] = []
        raw = getattr(r, "clause_ids_json", None) or "[]"
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                cids = [str(x) for x in parsed]
        except json.JSONDecodeError:
            cids = []
        citation_type = getattr(r, "citation_type", None) or "wiki"
        target_available = False
        if citation_type == "source" and getattr(r, "source_chunk_id", None) is not None:
            chunk = session.get(SourceChunk, r.source_chunk_id)
            chunk_space_id = (
                int(chunk.space_id) if chunk is not None and chunk.space_id is not None else default_space_id
            )
            target_available = chunk is not None and chunk_space_id == task_space_id
        elif citation_type == "wiki" and r.wiki_page_id is not None:
            page = session.get(WikiPageRow, r.wiki_page_id)
            page_space_id = (
                int(page.space_id) if page is not None and page.space_id is not None else default_space_id
            )
            target_available = page is not None and page_space_id == task_space_id
        legacy = not target_available
        out.append(
            TaskCitationOut(
                id=r.id,
                title=r.title,
                path=r.path,
                score=r.score,
                snippet=r.snippet,
                wiki_page_id=r.wiki_page_id,
                citation_type=citation_type,
                source_chunk_id=getattr(r, "source_chunk_id", None),
                content_excerpt=getattr(r, "content_excerpt", None) or "",
                clause_ids=cids,
                anchor_clause=getattr(r, "anchor_clause", None),
                available=target_available,
                legacy=legacy,
                legacy_reason=(
                    "引用目标已不可用，当前展示任务保存的历史摘录"
                    if legacy
                    else None
                ),
            )
        )
    return out


@router.get("/{task_id}/events", response_model=List[TaskEventOut])
def list_events(task_id: int, session: Session = Depends(get_session)) -> list[TaskEventOut]:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    rows = session.exec(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(col(TaskEvent.id).asc())
    ).all()
    try:
        space = resolve_space(session, task.wiki_space_id)
    except ValueError:
        space = None
    return [
        TaskEventOut(
            **row.model_dump(),
            wiki_space_id=task.wiki_space_id,
            wiki_space_name=space.name if space else "",
        )
        for row in rows
    ]


@router.get("/{task_id}/reviews", response_model=List[ReviewResultOut])
def list_reviews(
    task_id: int, session: Session = Depends(get_session)
) -> list[ReviewResultOut]:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    rows = session.exec(
        select(ReviewResult)
        .where(ReviewResult.task_id == task_id)
        .order_by(col(ReviewResult.id).desc())
    ).all()
    return [_review_to_out(r) for r in rows]


@router.get("/{task_id}/revisions", response_model=List[PromptRevisionOut])
def list_revisions(
    task_id: int, session: Session = Depends(get_session)
) -> list[PromptRevision]:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    rows = session.exec(
        select(PromptRevision)
        .where(PromptRevision.task_id == task_id)
        .order_by(col(PromptRevision.id).desc())
    ).all()
    return list(rows)
