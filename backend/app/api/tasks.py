from __future__ import annotations

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, col, select

from app.db import get_session
from app.models.entities import (
    CaseDraft,
    GenerationTask,
    Requirement,
    TaskCitation,
    TaskEvent,
)
from app.schemas.tasks import CaseDraftOut, TaskCreate, TaskEventOut, TaskOut
from app.services.task_pipeline import run_generate

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# Optional injectable chat_fn for tests: monkeypatch this module attr.
_GENERATE_CHAT_FN = None


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


def _citation_count(session: Session, task_id: int) -> int:
    rows = session.exec(select(TaskCitation).where(TaskCitation.task_id == task_id)).all()
    return len(rows)


def to_task_out(session: Session, task: GenerationTask) -> TaskOut:
    requirement = session.get(Requirement, task.requirement_id)
    draft = _latest_draft(session, task.id)
    snippet = None
    version = None
    if draft is not None:
        version = draft.version
        text = draft.content_md or ""
        snippet = text[:240]

    return TaskOut(
        id=task.id,
        requirement_id=task.requirement_id,
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
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.post("", response_model=TaskOut)
def create_task(
    body: TaskCreate,
    session: Session = Depends(get_session),
) -> TaskOut:
    requirement = Requirement(
        title=body.title,
        description=body.description,
        focus_tags_json=json.dumps(body.focus_tags or [], ensure_ascii=False),
    )
    session.add(requirement)
    session.commit()
    session.refresh(requirement)

    task = GenerationTask(
        requirement_id=requirement.id,
        status="draft",
        model_id=body.model_id,
        prompt_template_id=body.prompt_template_id,
    )
    session.add(task)
    session.commit()
    session.refresh(task)

    if body.run_generate:
        run_generate(session, task.id, chat_fn=_GENERATE_CHAT_FN)
        session.refresh(task)

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


@router.post("/{task_id}/generate", response_model=TaskOut)
def generate_task(task_id: int, session: Session = Depends(get_session)) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    task = run_generate(session, task_id, chat_fn=_GENERATE_CHAT_FN)
    if task.status == "failed":
        # Still return 200 with failed status so clients can inspect error_message/events;
        # raise 400 only when the task itself is missing (handled above).
        pass
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


@router.get("/{task_id}/events", response_model=List[TaskEventOut])
def list_events(task_id: int, session: Session = Depends(get_session)) -> list[TaskEvent]:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    rows = session.exec(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(col(TaskEvent.id).asc())
    ).all()
    return list(rows)
