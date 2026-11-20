from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from datetime import datetime, timezone
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlmodel import Session, col, select

from app.db import get_engine, get_session
from app.models.entities import (
    CaseDraft,
    DraftTestPointLink,
    GenerationTask,
    ModelConfig,
    Project,
    PromptRevision,
    PromptTemplate,
    Requirement,
    ReviewResult,
    TaskCitation,
    TaskRetrievalCheckpoint,
    TaskReferenceCase,
    TaskTestPointCheckpoint,
    TaskEvent,
    TestCase,
    TestPoint,
    TestPointCaseLink,
    TestPointCitation,
    WikiSpace,
)
from app.schemas.tasks import (
    ApplyPromptBody,
    CaseDraftOut,
    FinalizeConflictsOut,
    FinalizeTaskBody,
    PromptRevisionOut,
    ReviewResultOut,
    TaskCitationOut,
    TaskCreate,
    TaskEventOut,
    TaskModelUpdate,
    TaskOut,
    RetrievalCheckpointConfirm,
    RetrievalCheckpointOut,
    CoverageSummaryOut,
    RequirementOptimizeOut,
    RequirementOptimizeRequest,
    TaskReferenceCaseOut,
    TestPointCheckpointOut,
    TestPointConfirmRequest,
    TestPointEditRequest,
    TestPointInput,
    TestPointOut,
)
from app.services.permissions import Identity, current_user
from app.services.prompt_permissions import can_view_prompt
from app.services.task_events import append_event
from app.services.task_jobs import (
    job_generate,
    job_optimize_prompt,
    job_regenerate,
    job_review,
)
from app.services.task_locks import task_locks
from app.services.task_pipeline import (
    apply_prompt,
    finalize_task,
    run_generate,
    run_optimize_prompt,
    run_regenerate,
    run_review,
    _resolve_model,
    _fail_task,
)
from app.services.coverage import build_coverage
from app.services.case_management import (
    CaseKeyConflictError,
    aggregate_draft_cases_by_points,
    find_finalize_conflicts,
)
from app.services.requirement_optimizer import optimize_requirement
from app.services.test_points import (
    TEST_DIMENSIONS,
    current_points,
    latest_checkpoint as latest_test_point_checkpoint,
    point_citation_ids,
    task_dimensions,
    validate_point_inputs,
    write_points,
)
from app.services.task_state import InvalidTransition, transition
from app.services.task_stream import encode_sse, task_stream
from app.services.wiki_spaces import resolve_space, resolve_space_id

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# Optional injectable chat hooks for tests: monkeypatch these module attrs.
_PIPELINE_CHAT_FN = None
_GENERATE_CHAT_FN = None
_TEST_POINTS_CHAT_FN = None
_REVIEW_CHAT_FN = None
_OPTIMIZE_CHAT_FN = None
_REQUIREMENT_OPTIMIZE_CHAT_FN = None

# Statuses that mean a long-running job is already in flight
_BUSY = frozenset(
    {
        "retrieving",
        "generating_test_points",
        "generating",
        "reviewing",
        "optimizing",
        "regenerating",
    }
)
_STREAM_ACTIVE_STATUSES = frozenset({"retrieving", "generating_test_points", "generating", "regenerating"})
_STREAM_TERMINAL_STATUSES = frozenset({
    "generated",
    "reviewed",
    "finalized",
    "failed",
    "awaiting_confirmation",
    "awaiting_test_point_confirmation",
})
_STREAM_HEARTBEAT_SEC = 15.0
_STREAM_POLL_INTERVAL_SEC = 0.2


def _stream_terminal_for_status(status: str) -> str | None:
    if status == "failed":
        return "failed"
    if status in {"generated", "reviewed", "finalized"}:
        return "completed"
    if status in {"awaiting_confirmation", "awaiting_test_point_confirmation"}:
        return "completed"
    return None


def _load_stream_task_status(task_id: int) -> str:
    """Load only durable stream eligibility and close the DB session eagerly."""
    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return task.status


def _start_live_generate_stream(task: GenerationTask, message: str) -> None:
    """Create the live preview before the background task begins running."""
    task_stream.start(
        int(task.id or 0),
        status=task.status,
        message=message,
    )


def _chat_for(stage: str = "generate"):
    """Resolve chat hook for a pipeline stage."""
    if stage == "review" and _REVIEW_CHAT_FN is not None:
        return _REVIEW_CHAT_FN
    if stage == "optimize" and _OPTIMIZE_CHAT_FN is not None:
        return _OPTIMIZE_CHAT_FN
    if stage == "generate" and _GENERATE_CHAT_FN is not None:
        return _GENERATE_CHAT_FN
    if stage == "test_points" and _TEST_POINTS_CHAT_FN is not None:
        return _TEST_POINTS_CHAT_FN
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


def _request_operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return str(getattr(user, "username", None) or "system")[:128]


def _latest_review_row(session: Session, task_id: int) -> Optional[ReviewResult]:
    return session.exec(
        select(ReviewResult)
        .where(ReviewResult.task_id == task_id)
        .order_by(col(ReviewResult.id).desc())
    ).first()


def _citation_count(session: Session, task_id: int) -> int:
    rows = session.exec(select(TaskCitation).where(TaskCitation.task_id == task_id)).all()
    return len(rows)


def _json_string_list(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        parsed = []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _json_dict(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _task_citation_out(
    row: TaskCitation,
    *,
    available: bool = True,
    legacy: bool = False,
    legacy_reason: str | None = None,
) -> TaskCitationOut:
    return TaskCitationOut(
        id=row.id,
        title=row.title,
        path=row.path,
        score=row.score,
        snippet=row.snippet,
        wiki_page_id=row.wiki_page_id,
        citation_type=getattr(row, "citation_type", "external") or "external",
        source_chunk_id=getattr(row, "source_chunk_id", None),
        content_excerpt=getattr(row, "content_excerpt", "") or "",
        clause_ids=_json_string_list(getattr(row, "clause_ids_json", "[]")),
        anchor_clause=getattr(row, "anchor_clause", None),
        knowledge_source_id=getattr(row, "knowledge_source_id", None),
        file_path=getattr(row, "file_path", None) or row.path or None,
        heading_path=getattr(row, "heading_path", None),
        external_metadata=_json_dict(getattr(row, "external_metadata_json", None)),
        available=available,
        legacy=legacy,
        legacy_reason=legacy_reason,
    )


def _checkpoint_out(session: Session, checkpoint: TaskRetrievalCheckpoint) -> RetrievalCheckpointOut:
    selected = [int(x) for x in json.loads(checkpoint.selected_citation_ids_json or "[]")]
    candidates = [int(x) for x in json.loads(checkpoint.candidate_citation_ids_json or "[]")]
    rows = session.exec(select(TaskCitation).where(TaskCitation.id.in_(candidates)).order_by(col(TaskCitation.id))).all()
    available = {row.id: _task_citation_out(row) for row in rows}
    try:
        snapshot = json.loads(checkpoint.retrieval_json or "{}")
    except json.JSONDecodeError:
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    raw_warnings = snapshot.get("source_warnings")
    warnings = [item for item in raw_warnings if isinstance(item, dict)] if isinstance(raw_warnings, list) else []
    return RetrievalCheckpointOut(
        id=checkpoint.id, task_id=checkpoint.task_id, attempt=checkpoint.attempt,
        version=checkpoint.version, status=checkpoint.status, query=checkpoint.query,
        auto_review=bool(checkpoint.auto_review),
        candidate_citations=[available[row.id] for row in rows if row.id in available],
        selected_citation_ids=selected, supplemental_text=checkpoint.supplemental_text or "",
        idempotency_key=checkpoint.idempotency_key, created_at=checkpoint.created_at, updated_at=checkpoint.updated_at,
        conflict_groups=snapshot.get("conflict_groups") or [],
        knowledge_status=str(snapshot.get("knowledge_status") or "ready"),
        knowledge_message=str(snapshot.get("knowledge_message") or ""),
        source_warnings=warnings,
    )


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


_PROMPT_REF_ID_RE = re.compile(r"^id:(\d+):")
_PRIVATE_TASK_PROMPT_FORBIDDEN_DETAIL = (
    "该任务绑定了其他用户的私有提示词模板，当前账号无权操作"
)


def _is_prompt_id_visible(
    session: Session,
    prompt_id: int | None,
    user: Identity | None,
    cache: dict[int, bool] | None = None,
) -> bool:
    """Return True when prompt_id is unset, missing, or visible to ``user``."""
    if prompt_id is None:
        return True
    pid = int(prompt_id)
    if cache is not None and pid in cache:
        return cache[pid]
    row = session.get(PromptTemplate, pid)
    visible = True if row is None else can_view_prompt(row, user)
    if cache is not None:
        cache[pid] = visible
    return visible


def _prompt_id_from_ref(ref: str | None) -> int | None:
    if not ref:
        return None
    match = _PROMPT_REF_ID_RE.match(str(ref).strip())
    if not match:
        return None
    return int(match.group(1))


def _request_user(request: Request | None) -> Identity | None:
    return current_user(request) if request is not None else None


def _ensure_task_prompt_accessible(
    session: Session,
    task: GenerationTask,
    request: Request | None,
) -> None:
    """Reject operations on a task bound to another user's private prompt."""
    if task.prompt_template_id is None:
        return
    prompt = session.get(PromptTemplate, task.prompt_template_id)
    if prompt is not None and not can_view_prompt(prompt, _request_user(request)):
        raise HTTPException(
            status_code=403,
            detail=_PRIVATE_TASK_PROMPT_FORBIDDEN_DETAIL,
        )


def _sanitize_prompt_ref(
    session: Session,
    ref: str | None,
    user: Identity | None,
    *,
    task_prompt_visible: bool,
    cache: dict[int, bool],
) -> str | None:
    if ref is None:
        return None
    if not task_prompt_visible:
        return "private"
    ref_pid = _prompt_id_from_ref(ref)
    if ref_pid is not None and not _is_prompt_id_visible(session, ref_pid, user, cache):
        return "private"
    return ref


def _sanitize_event_detail_json(
    session: Session,
    detail_json: str | None,
    user: Identity | None,
    *,
    task_prompt_visible: bool,
    cache: dict[int, bool],
) -> str | None:
    if not detail_json:
        return detail_json
    try:
        payload = json.loads(detail_json)
    except (TypeError, ValueError):
        return detail_json
    if not isinstance(payload, dict):
        return detail_json

    mutated = False
    for id_key in ("base_prompt_id", "prompt_template_id"):
        if id_key in payload:
            val = payload[id_key]
            is_hidden_id = (
                isinstance(val, int)
                and not isinstance(val, bool)
                and not _is_prompt_id_visible(session, val, user, cache)
            )
            if is_hidden_id or (not task_prompt_visible and val is not None):
                payload.pop(id_key, None)
                mutated = True

    for ref_key in ("prompt_ref", "prompt_version_ref"):
        if ref_key in payload:
            val = payload[ref_key]
            ref_pid = _prompt_id_from_ref(val if isinstance(val, str) else None)
            if not task_prompt_visible or (
                ref_pid is not None and not _is_prompt_id_visible(session, ref_pid, user, cache)
            ):
                payload[ref_key] = "private"
                mutated = True

    if not mutated:
        return detail_json
    return json.dumps(payload, ensure_ascii=False)


def to_task_out(
    session: Session,
    task: GenerationTask,
    *,
    user: Identity | None = None,
    prompt_visibility_cache: dict[int, bool] | None = None,
) -> TaskOut:
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
    reference_count = len(
        session.exec(
            select(TaskReferenceCase).where(TaskReferenceCase.task_id == task.id)
        ).all()
    )
    test_point_count = len(current_points(session, task.id))
    try:
        space = resolve_space(session, task.wiki_space_id)
    except ValueError:
        space = None

    visible_prompt_template_id = (
        task.prompt_template_id
        if _is_prompt_id_visible(
            session,
            task.prompt_template_id,
            user,
            prompt_visibility_cache,
        )
        else None
    )

    return TaskOut(
        id=task.id,
        requirement_id=task.requirement_id,
        project_id=task.project_id,
        wiki_space_id=int(space.id if space and space.id is not None else task.wiki_space_id or 0),
        wiki_space_name=space.name if space else "",
        status=task.status,
        model_id=task.model_id,
        prompt_template_id=visible_prompt_template_id,
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
        generation_granularity=getattr(task, "generation_granularity", None) or "standard",
        test_dimensions=task_dimensions(task),
        reference_case_count=reference_count,
        test_point_count=test_point_count,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.post("/requirement-optimize", response_model=RequirementOptimizeOut)
def optimize_requirement_route(
    body: RequirementOptimizeRequest,
    session: Session = Depends(get_session),
) -> RequirementOptimizeOut:
    """Suggest editable requirement copy without touching the legacy Prompt optimizer."""

    try:
        result = optimize_requirement(
            session,
            title=body.title,
            description=body.description,
            focus_tags=body.focus_tags,
            model_id=body.model_id,
            chat_fn=_REQUIREMENT_OPTIMIZE_CHAT_FN,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Requirement optimization failed: {exc}") from exc
    return RequirementOptimizeOut(**result)


@router.post("", response_model=TaskOut)
def create_task(
    body: TaskCreate,
    background_tasks: BackgroundTasks,
    request: Request,
    session: Session = Depends(get_session),
    wait: bool = Query(
        False,
        description="If true (or chat hooks injected), run generate inline before responding",
    ),
) -> TaskOut:
    space: WikiSpace | None = None
    if body.wiki_space_id is not None:
        try:
            space = resolve_space(session, body.wiki_space_id, for_write=True)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        project_id = body.project_id or space.project_id
    elif body.project_id is not None:
        project_row = session.get(Project, body.project_id)
        if project_row is None:
            raise HTTPException(status_code=422, detail="Project not found")
        project_id = int(project_row.id)
        if project_row.default_wiki_space_id is not None:
            candidate = session.get(WikiSpace, int(project_row.default_wiki_space_id))
            if candidate is not None and candidate.status == "active" and candidate.scope == "project":
                space = candidate
        if space is None:
            space = session.exec(
                select(WikiSpace)
                .where(
                    WikiSpace.project_id == project_id,
                    WikiSpace.scope == "project",
                    WikiSpace.status == "active",
                )
                .order_by(WikiSpace.id.asc())
            ).first()
    else:
        try:
            space = resolve_space(session, None, for_write=True)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        project_id = space.project_id

    if project_id is None or space is None or space.scope != "project":
        raise HTTPException(status_code=422, detail="Tasks require a project and a project Wiki space")
    try:
        from app.services.projects import require_request_project, validate_project_space
        require_request_project(request, project_id)
        validate_project_space(session, project_id, int(space.id or 0))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.prompt_template_id is not None:
        prompt = session.get(PromptTemplate, body.prompt_template_id)
        if prompt is None or not can_view_prompt(prompt, current_user(request)):
            raise HTTPException(status_code=404, detail="Prompt not found")
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

    dimensions = body.dimensions if body.dimensions is not None else body.test_dimensions
    dimensions = list(dict.fromkeys(str(item).strip().lower() for item in dimensions if str(item).strip()))
    if not dimensions:
        dimensions = ["positive", "negative", "boundary"]
    unknown_dimensions = [item for item in dimensions if item not in TEST_DIMENSIONS]
    if unknown_dimensions:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown test dimensions: {', '.join(unknown_dimensions)}",
        )
    reference_ids = list(dict.fromkeys(int(item) for item in body.reference_case_ids))
    if len(reference_ids) > 10:
        raise HTTPException(status_code=422, detail="At most 10 reference cases may be selected")
    reference_rows: list[TestCase] = []
    if reference_ids:
        reference_rows = list(
            session.exec(select(TestCase).where(TestCase.id.in_(reference_ids))).all()
        )
        by_id = {int(row.id): row for row in reference_rows if row.id is not None}
        missing = [str(item) for item in reference_ids if item not in by_id]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"Reference case not found: {', '.join(missing)}",
            )
        inactive = [row.case_key for row in reference_rows if row.status != "active"]
        if inactive:
            raise HTTPException(
                status_code=422,
                detail=f"Only active cases can be used as references: {', '.join(inactive)}",
            )
    reference_text = body.reference_text.strip()
    if len(reference_text) > 16000:
        raise HTTPException(status_code=422, detail="Manual reference text is too long")
    snapshot_chars = sum(len(row.content_md or "") for row in reference_rows) + len(reference_text)
    if snapshot_chars > 30000:
        raise HTTPException(status_code=422, detail="Reference case snapshots exceed 30000 characters")

    if body.requirement_id is not None:
        requirement = session.get(Requirement, body.requirement_id)
        if requirement is None:
            raise HTTPException(status_code=422, detail="Requirement not found")
        if requirement.project_id is not None and requirement.project_id != project_id:
            raise HTTPException(status_code=422, detail="Requirement belongs to another project")
        requirement.project_id = project_id
        if body.title is not None and body.title.strip():
            requirement.title = body.title.strip()
        if body.description is not None and body.description.strip():
            requirement.description = body.description.strip()
        if body.focus_tags is not None:
            requirement.focus_tags_json = json.dumps(body.focus_tags, ensure_ascii=False)
        session.add(requirement)
        session.commit()
        session.refresh(requirement)
    else:
        if not (body.title or "").strip() or not (body.description or "").strip():
            raise HTTPException(
                status_code=422,
                detail="title and description are required when requirement_id is omitted",
            )
        requirement = Requirement(
            project_id=project_id,
            title=(body.title or "").strip(),
            description=(body.description or "").strip(),
            focus_tags_json=json.dumps(body.focus_tags or [], ensure_ascii=False),
            source_filename=body.source_filename or None,
        )
        session.add(requirement)
        session.commit()
        session.refresh(requirement)

    task = GenerationTask(
        requirement_id=requirement.id,
        project_id=project_id,
        wiki_space_id=space.id,
        status="draft",
        model_id=body.model_id,
        prompt_template_id=body.prompt_template_id,
        generation_granularity=body.generation_granularity,
        test_dimensions_json=json.dumps(dimensions, ensure_ascii=False),
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    for row in reference_rows:
        content = row.content_md or ""
        session.add(
            TaskReferenceCase(
                task_id=int(task.id),
                source_case_id=row.id,
                source_case_key=row.case_key,
                title_snapshot=row.title or row.case_key,
                content_md_snapshot=content,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                source="case_library",
            )
        )
    if reference_text:
        session.add(
            TaskReferenceCase(
                task_id=int(task.id),
                source_case_id=None,
                source_case_key=None,
                title_snapshot="手动输入",
                content_md_snapshot=reference_text,
                content_hash=hashlib.sha256(reference_text.encode("utf-8")).hexdigest(),
                source="manual",
            )
        )
    session.commit()
    session.refresh(task)
    # SQLite may reuse an integer id after deletion. Clear retained state so a
    # new task cannot inherit old preview text; draft-only tasks allocate no
    # broker capacity until generation actually starts.
    task_stream.discard(int(task.id or 0))

    if body.run_generate:
        if _force_sync("generate", wait):
            _start_live_generate_stream(task, "开始生成测试用例")
            run_generate(session, task.id, chat_fn=_chat_for("generate"), auto_review=body.auto_review)
            session.refresh(task)
            if body.auto_review and task.status == "generated":
                run_review(session, task.id, chat_fn=_chat_for("review"))
                session.refresh(task)
        else:
            task = _begin_status(
                session, task, "retrieving", "retrieve", "任务已创建，后台开始检索/生成"
            )
            _start_live_generate_stream(task, "后台开始检索/生成")
            background_tasks.add_task(
                job_generate, task.id, bool(body.auto_review)
            )

    return to_task_out(session, task, user=current_user(request))


@router.get("", response_model=List[TaskOut])
def list_tasks(
    project_id: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
    request: Request = None,
) -> list[TaskOut]:
    statement = select(GenerationTask)
    if project_id is not None:
        statement = statement.where(GenerationTask.project_id == project_id)
    rows = session.exec(statement.order_by(col(GenerationTask.id).desc())).all()
    user = _request_user(request)
    visibility_cache: dict[int, bool] = {}
    return [
        to_task_out(session, r, user=user, prompt_visibility_cache=visibility_cache)
        for r in rows
    ]


@router.get("/{task_id}/stream")
def stream_task(task_id: int) -> StreamingResponse:
    """Subscribe to the live generation preview for one task.

    The first event is always a complete snapshot, so reconnecting clients can
    safely replace their local preview rather than depend on retained deltas.
    """
    task_status = _load_stream_task_status(task_id)
    if task_status not in _STREAM_ACTIVE_STATUSES | _STREAM_TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Task status {task_status!r} has no active generation stream",
        )

    durable_terminal = _stream_terminal_for_status(task_status)
    snapshot = task_stream.snapshot(task_id)
    if snapshot is None and task_status in _STREAM_ACTIVE_STATUSES:
        # After process restart/LRU eviction there is no local producer state
        # to follow. Do not manufacture a stream that can only heartbeat;
        # polling the durable task status remains authoritative.
        raise HTTPException(
            status_code=409,
            detail="Live generation stream unavailable; use task polling",
        )
    if snapshot is None:
        task_stream.ensure(task_id, status=task_status, terminal=durable_terminal)
        snapshot = task_stream.snapshot(task_id)
    # ``ensure`` above creates the state unless it is already live, but keep a
    # defensively complete snapshot if a broker implementation ever evicts it
    # between those two operations.
    if snapshot is None:
        snapshot = {
            "stream_id": 0,
            "sequence": 0,
            "status": task_status,
            "text": "",
            "terminal": durable_terminal,
            "truncated": False,
        }
    if durable_terminal is not None and snapshot.get("terminal") is None:
        # The durable status wins during the tiny interval after the DB commit
        # and before the producer publishes its in-memory terminal event.
        snapshot = {
            **snapshot,
            "status": task_status,
            "terminal": durable_terminal,
        }

    async def event_stream() -> AsyncIterator[str]:
        sequence = int(snapshot["sequence"])
        stream_id = int(snapshot.get("stream_id") or 0)
        next_heartbeat_at = time.monotonic() + _STREAM_HEARTBEAT_SEC
        yield encode_sse("snapshot", snapshot, sequence=sequence)

        if snapshot["terminal"] in {"completed", "failed"}:
            return

        # The snapshot is authoritative on every connection.  Starting after
        # it avoids replaying deltas already represented by ``snapshot.text``;
        # EventSource's Last-Event-ID is therefore naturally superseded by the
        # snapshot when a client reconnects.
        while True:
            result = task_stream.poll_after(
                task_id,
                sequence,
                stream_id=stream_id,
            )
            if result.kind == "missing":
                # Eviction, expiry, deletion/recreation, or another lifecycle
                # replacement. Closing lets EventSource reconnect and the UI's
                # existing polling fallback remains authoritative.
                return
            if result.kind == "timeout":
                now = time.monotonic()
                if now >= next_heartbeat_at:
                    yield ": keep-alive\n\n"
                    next_heartbeat_at = now + _STREAM_HEARTBEAT_SEC
                # Async polling avoids holding a Starlette worker thread while
                # the model is generating. No database session remains open.
                await asyncio.sleep(_STREAM_POLL_INTERVAL_SEC)
                continue
            if result.kind == "snapshot":
                current = result.snapshot
                if current is None:
                    return
                sequence = int(current["sequence"])
                stream_id = int(current.get("stream_id") or stream_id)
                yield encode_sse("snapshot", current, sequence=sequence)
                if current.get("terminal") in {"completed", "failed"}:
                    return
                continue

            for item in result.events:
                sequence = item.sequence
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


@router.get("/{task_id}/retrieval-checkpoint", response_model=RetrievalCheckpointOut)
def get_retrieval_checkpoint(task_id: int, session: Session = Depends(get_session)) -> RetrievalCheckpointOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    checkpoint = session.exec(
        select(TaskRetrievalCheckpoint)
        .where(TaskRetrievalCheckpoint.task_id == task_id)
        .order_by(col(TaskRetrievalCheckpoint.attempt).desc())
    ).first()
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="Retrieval checkpoint not found")
    return _checkpoint_out(session, checkpoint)


@router.post("/{task_id}/retrieval-checkpoint/confirm", response_model=TaskOut)
def confirm_retrieval_checkpoint(
    task_id: int,
    body: RetrievalCheckpointConfirm,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    request: Request = None,
) -> TaskOut:
    with task_locks.hold(task_id):
        task = session.get(GenerationTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        checkpoint = session.exec(
            select(TaskRetrievalCheckpoint)
            .where(TaskRetrievalCheckpoint.task_id == task_id)
            .order_by(col(TaskRetrievalCheckpoint.attempt).desc())
        ).first()
        if checkpoint is None:
            raise HTTPException(status_code=404, detail="Retrieval checkpoint not found")
        selected = list(dict.fromkeys(int(x) for x in body.selected_citation_ids))
        candidate_ids = set(int(x) for x in json.loads(checkpoint.candidate_citation_ids_json or "[]"))
        actual_ids = {int(row.id) for row in session.exec(select(TaskCitation).where(TaskCitation.task_id == task_id, TaskCitation.id.in_(candidate_ids))).all()}
        if any(x not in actual_ids for x in selected):
            raise HTTPException(status_code=422, detail="Selected citation does not belong to checkpoint")
        if not selected and not body.supplemental_text.strip() and not body.allow_empty_context:
            raise HTTPException(
                status_code=422,
                detail="Select a citation, provide supplemental context, or confirm empty context",
            )
        try:
            snapshot = json.loads(checkpoint.retrieval_json or "{}")
        except (AttributeError, json.JSONDecodeError):
            snapshot = {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        knowledge_status = str(snapshot.get("knowledge_status") or "ready")
        payload_hash = hashlib.sha256(
            json.dumps(
                {
                    "selected": selected,
                    "supplemental_text": body.supplemental_text,
                    "allow_empty_context": bool(body.allow_empty_context),
                    "knowledge_status": knowledge_status,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if checkpoint.status == "confirmed":
            if checkpoint.decision_hash == payload_hash and checkpoint.idempotency_key == body.idempotency_key:
                return to_task_out(session, task, user=_request_user(request))
            raise HTTPException(status_code=409, detail="Checkpoint already confirmed")
        if checkpoint.version != body.expected_version:
            raise HTTPException(status_code=409, detail="Stale retrieval checkpoint version")
        checkpoint.status = "confirmed"
        checkpoint.selected_citation_ids_json = json.dumps(selected)
        checkpoint.supplemental_text = body.supplemental_text.strip()
        checkpoint.decision_hash = payload_hash
        checkpoint.idempotency_key = body.idempotency_key
        checkpoint.resume_claim_token = secrets.token_urlsafe(24)
        checkpoint.resume_claimed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        checkpoint.resume_status = "claimed"
        session.add(checkpoint)
        task.status = transition(task.status, "generating_test_points")
        task.error_message = None
        session.add(task)
        append_event(
            session,
            task.id,
            "retrieve",
            "人工确认检索结果，开始生成测试点",
            detail={
                "selected_citation_ids": selected,
                "allow_empty_context": bool(body.allow_empty_context),
                "knowledge_status": knowledge_status,
            },
        )
        session.commit()
        session.refresh(task)
        _start_live_generate_stream(task, "已确认检索结果，开始生成测试点")
        background_tasks.add_task(
            job_generate,
            task_id,
            bool(checkpoint.auto_review),
            checkpoint.id,
            checkpoint.resume_claim_token,
            "retrieval",
        )
        return to_task_out(session, task, user=_request_user(request))


def _test_point_out(session: Session, row: TestPoint) -> TestPointOut:
    return TestPointOut(
        id=int(row.id or 0),
        task_id=int(row.task_id),
        checkpoint_id=int(row.checkpoint_id),
        stable_key=row.stable_key,
        title=row.title,
        verification_goal=row.verification_goal,
        dimension=row.dimension,
        priority=row.priority,
        sort_order=int(row.sort_order),
        is_selected=bool(row.is_selected),
        is_excluded=bool(row.is_excluded),
        citation_ids=point_citation_ids(session, int(row.id)),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _test_point_checkpoint_out(
    session: Session, checkpoint: TaskTestPointCheckpoint
) -> TestPointCheckpointOut:
    rows = session.exec(
        select(TestPoint)
        .where(TestPoint.checkpoint_id == checkpoint.id)
        .order_by(col(TestPoint.sort_order).asc(), col(TestPoint.id).asc())
    ).all()
    return TestPointCheckpointOut(
        id=int(checkpoint.id or 0),
        task_id=int(checkpoint.task_id),
        retrieval_checkpoint_id=checkpoint.retrieval_checkpoint_id,
        attempt=int(checkpoint.attempt),
        version=int(checkpoint.version),
        status=checkpoint.status,
        points=[_test_point_out(session, row) for row in rows],
        idempotency_key=checkpoint.idempotency_key,
        created_at=checkpoint.created_at,
        updated_at=checkpoint.updated_at,
    )


def _point_payload_hash(points: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(points, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


@router.get("/{task_id}/test-points", response_model=TestPointCheckpointOut)
def get_test_points(
    task_id: int, session: Session = Depends(get_session)
) -> TestPointCheckpointOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    checkpoint = latest_test_point_checkpoint(session, task_id)
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="Test-point checkpoint not found")
    return _test_point_checkpoint_out(session, checkpoint)


@router.put("/{task_id}/test-points", response_model=TestPointCheckpointOut)
def edit_test_points(
    task_id: int,
    body: TestPointEditRequest,
    session: Session = Depends(get_session),
) -> TestPointCheckpointOut:
    with task_locks.hold(task_id):
        task = session.get(GenerationTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status != "awaiting_test_point_confirmation":
            raise HTTPException(status_code=409, detail="Test points are not editable in the current task state")
        checkpoint = latest_test_point_checkpoint(session, task_id)
        if checkpoint is None:
            raise HTTPException(status_code=404, detail="Test-point checkpoint not found")
        if checkpoint.status != "pending":
            raise HTTPException(status_code=409, detail="Test-point checkpoint is already confirmed")
        if checkpoint.version != body.expected_version:
            raise HTTPException(status_code=409, detail="Stale test-point checkpoint version")
        try:
            normalized = validate_point_inputs(
                session,
                task,
                [item.model_dump() for item in body.points],
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        write_points(session, task, checkpoint, normalized)
        checkpoint.version += 1
        checkpoint.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.add(checkpoint)
        append_event(session, task.id, "test_points", "人工编辑测试点", detail={"count": len(normalized)})
        session.commit()
        session.refresh(checkpoint)
        return _test_point_checkpoint_out(session, checkpoint)


@router.post("/{task_id}/test-points/confirm", response_model=TaskOut)
def confirm_test_points(
    task_id: int,
    body: TestPointConfirmRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    request: Request = None,
) -> TaskOut:
    with task_locks.hold(task_id):
        task = session.get(GenerationTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        _ensure_task_prompt_accessible(session, task, request)
        checkpoint = latest_test_point_checkpoint(session, task_id)
        if checkpoint is None:
            raise HTTPException(status_code=404, detail="Test-point checkpoint not found")
        try:
            normalized = validate_point_inputs(
                session,
                task,
                [item.model_dump() for item in body.points],
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        payload_hash = _point_payload_hash(normalized)
        if checkpoint.status == "confirmed":
            if checkpoint.decision_hash == payload_hash and checkpoint.idempotency_key == body.idempotency_key:
                return to_task_out(session, task, user=_request_user(request))
            raise HTTPException(status_code=409, detail="Test-point checkpoint already confirmed")
        if task.status != "awaiting_test_point_confirmation":
            raise HTTPException(status_code=409, detail="Task is not waiting for test-point confirmation")
        if checkpoint.version != body.expected_version:
            raise HTTPException(status_code=409, detail="Stale test-point checkpoint version")
        write_points(session, task, checkpoint, normalized)
        checkpoint.status = "confirmed"
        checkpoint.decision_hash = payload_hash
        checkpoint.idempotency_key = body.idempotency_key
        checkpoint.resume_claim_token = secrets.token_urlsafe(24)
        checkpoint.resume_claimed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        checkpoint.resume_status = "claimed"
        session.add(checkpoint)
        task.status = transition(task.status, "generating")
        task.error_message = None
        session.add(task)
        append_event(
            session,
            task.id,
            "test_points",
            "人工确认测试点，开始生成完整用例",
            detail={"checkpoint_id": checkpoint.id, "selected_count": sum(1 for item in normalized if item["is_selected"] and not item["is_excluded"])},
        )
        session.commit()
        session.refresh(task)
        _start_live_generate_stream(task, "已确认测试点，开始生成完整用例")
        background_tasks.add_task(
            job_generate,
            task_id,
            bool(checkpoint.auto_review),
            checkpoint.id,
            checkpoint.resume_claim_token,
            "test_points",
        )
        return to_task_out(session, task, user=_request_user(request))


@router.get("/{task_id}/references", response_model=List[TaskReferenceCaseOut])
def list_task_references(
    task_id: int, session: Session = Depends(get_session)
) -> list[TaskReferenceCaseOut]:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    rows = session.exec(
        select(TaskReferenceCase)
        .where(TaskReferenceCase.task_id == task_id)
        .order_by(col(TaskReferenceCase.id).asc())
    ).all()
    return [TaskReferenceCaseOut.model_validate(row) for row in rows]


@router.get("/{task_id}/coverage", response_model=CoverageSummaryOut)
def task_coverage(task_id: int, session: Session = Depends(get_session)) -> CoverageSummaryOut:
    if session.get(GenerationTask, task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return CoverageSummaryOut(**build_coverage(session, task_id))


@router.get("/{task_id}", response_model=TaskOut)
def get_task(
    task_id: int,
    session: Session = Depends(get_session),
    request: Request = None,
) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return to_task_out(session, task, user=_request_user(request))


@router.patch("/{task_id}/model", response_model=TaskOut)
def update_task_model(
    task_id: int,
    body: TaskModelUpdate,
    session: Session = Depends(get_session),
    request: Request = None,
) -> TaskOut:
    """Change the model before an initial or failed generation attempt."""
    with task_locks.hold(task_id):
        session.expire_all()
        return _update_task_model_locked(task_id, body, session, user=_request_user(request))


def _update_task_model_locked(
    task_id: int,
    body: TaskModelUpdate,
    session: Session,
    *,
    user: Identity | None = None,
) -> TaskOut:

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
    return to_task_out(session, task, user=user)


@router.post("/{task_id}/stop", response_model=TaskOut)
def stop_task(
    task_id: int,
    session: Session = Depends(get_session),
    request: Request = None,
) -> TaskOut:
    """Manually stop a generating task and release its durable resume lease."""
    with task_locks.hold(task_id):
        session.expire_all()
        return _stop_task_locked(task_id, session, user=_request_user(request))


def _stop_task_locked(
    task_id: int,
    session: Session,
    *,
    user: Identity | None = None,
) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "generating":
        raise HTTPException(status_code=409, detail="仅生成中的任务可以停止")

    # ``failed`` has no outgoing edge to ``generated``: the still-running
    # worker polls the row between LLM calls (see task_pipeline) and exits
    # cooperatively instead of overwriting this terminal state.
    task.status = transition(task.status, "failed")
    task.error_message = "已手动停止生成"
    task.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(task)
    append_event(session, task.id, "generate", "用户手动停止生成")
    checkpoint = session.exec(
        select(TaskTestPointCheckpoint)
        .where(TaskTestPointCheckpoint.task_id == task_id)
        .where(TaskTestPointCheckpoint.status == "confirmed")
        .order_by(col(TaskTestPointCheckpoint.attempt).desc())
    ).first()
    if checkpoint is not None:
        # Release the lease: resume_status="stopped" is neither claimed|running
        # (so recovery no longer treats it as a fresh claim) nor one of the
        # reclaimable states (NULL/stale claim/completed), and the failed task
        # is never selected by recover_generation_jobs anyway.  The token is
        # kept for traceability; recovery would re-issue a fresh token first.
        checkpoint.resume_status = "stopped"
        checkpoint.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.add(checkpoint)
    session.commit()
    session.refresh(task)
    # Publish post-commit so the SSE terminal event agrees with durable state
    # (same ordering as _fail_task in the pipeline).
    task_stream.fail(task_id, message="已手动停止生成")
    return to_task_out(session, task, user=user)


@router.delete("/{task_id}")
def delete_task(task_id: int, session: Session = Depends(get_session)) -> dict:
    """Hard-delete a task and its dependent rows (drafts, reviews, events, …)."""
    with task_locks.hold(task_id):
        session.expire_all()
        return _delete_task_locked(task_id, session)


def _delete_task_locked(task_id: int, session: Session) -> dict:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status in _BUSY:
        # Background jobs address tasks by integer id. Prevent deletion and
        # SQLite id reuse while a queued/running job could still act on it.
        raise HTTPException(status_code=409, detail="Cannot delete a task while it is running")

    requirement_id = task.requirement_id
    stream_snapshot = task_stream.snapshot(task_id)
    expected_stream_id = (
        int(stream_snapshot["stream_id"])
        if stream_snapshot is not None
        else None
    )

    reference_rows = session.exec(
        select(TaskReferenceCase).where(TaskReferenceCase.task_id == task_id)
    ).all()
    for row in reference_rows:
        session.delete(row)
    point_checkpoints = session.exec(
        select(TaskTestPointCheckpoint).where(TaskTestPointCheckpoint.task_id == task_id)
    ).all()
    point_rows = session.exec(select(TestPoint).where(TestPoint.task_id == task_id)).all()
    for point in point_rows:
        for link in session.exec(
            select(TestPointCitation).where(TestPointCitation.test_point_id == point.id)
        ).all():
            session.delete(link)
        for link in session.exec(
            select(TestPointCaseLink).where(TestPointCaseLink.test_point_id == point.id)
        ).all():
            session.delete(link)
        for link in session.exec(
            select(DraftTestPointLink).where(DraftTestPointLink.test_point_id == point.id)
        ).all():
            session.delete(link)
        session.delete(point)
    for model in (TaskCitation, TaskRetrievalCheckpoint, CaseDraft, ReviewResult, PromptRevision, TaskEvent):
        rows = session.exec(select(model).where(model.task_id == task_id)).all()
        for row in rows:
            session.delete(row)
    for checkpoint in point_checkpoints:
        session.delete(checkpoint)

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
    if expected_stream_id is not None:
        task_stream.fail_if_active(
            task_id,
            message="任务已删除，实时预览已关闭",
            expected_stream_id=expected_stream_id,
        )
    return {"ok": True, "id": task_id}


@router.post("/{task_id}/generate", response_model=TaskOut)
def generate_task(
    task_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    wait: bool = Query(False),
    auto_review: bool = Query(False),
    request: Request = None,
) -> TaskOut:
    with task_locks.hold(task_id):
        session.expire_all()
        return _generate_task_locked(
            task_id,
            background_tasks,
            session,
            request=request,
            wait=wait,
            auto_review=auto_review,
        )


def _generate_task_locked(
    task_id: int,
    background_tasks: BackgroundTasks,
    session: Session,
    *,
    request: Request | None,
    wait: bool,
    auto_review: bool,
) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    _ensure_task_prompt_accessible(session, task, request)
    user = _request_user(request)

    if task.status in _BUSY:
        # Already running — do not double-schedule
        return to_task_out(session, task, user=user)

    if task.status in {"awaiting_confirmation", "awaiting_test_point_confirmation"}:
        return to_task_out(session, task, user=user)

    try:
        _resolve_model(session, task)
    except Exception as exc:  # noqa: BLE001
        _start_live_generate_stream(task, "生成失败")
        return to_task_out(
            session,
            _fail_task(session, task, str(exc), publish_stream=True),
            user=user,
        )

    if _force_sync("generate", wait):
        _start_live_generate_stream(task, "开始生成测试用例")
        task = run_generate(session, task_id, chat_fn=_chat_for("generate"), auto_review=auto_review)
        if auto_review and task.status == "generated":
            task = run_review(session, task_id, chat_fn=_chat_for("review"))
        return to_task_out(session, task, user=user)

    if task.status not in ("draft", "failed", "regenerating"):
        # Preserve envelope: pipeline marks failed + error_message
        _start_live_generate_stream(task, "开始生成测试用例")
        task = run_generate(session, task_id, chat_fn=_chat_for("generate"), auto_review=auto_review)
        return to_task_out(session, task, user=user)

    task = _begin_status(
        session, task, "retrieving", "retrieve", "后台开始检索 Wiki / 原文"
    )
    _start_live_generate_stream(task, "后台开始检索 Wiki / 原文")
    background_tasks.add_task(job_generate, task_id, auto_review)
    return to_task_out(session, task, user=user)


@router.post("/{task_id}/review", response_model=TaskOut)
def review_task(
    task_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    wait: bool = Query(False),
    request: Request = None,
) -> TaskOut:
    with task_locks.hold(task_id):
        session.expire_all()
        return _review_task_locked(
            task_id,
            background_tasks,
            session,
            user=_request_user(request),
            wait=wait,
        )


def _review_task_locked(
    task_id: int,
    background_tasks: BackgroundTasks,
    session: Session,
    *,
    user: Identity | None = None,
    wait: bool,
) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status in _BUSY:
        return to_task_out(session, task, user=user)

    if task.status in {"awaiting_confirmation", "awaiting_test_point_confirmation"}:
        return to_task_out(session, task, user=user)

    if _force_sync("review", wait):
        task = run_review(session, task_id, chat_fn=_chat_for("review"))
        return to_task_out(session, task, user=user)

    if task.status not in ("generated", "failed", "reviewed"):
        task = run_review(session, task_id, chat_fn=_chat_for("review"))
        return to_task_out(session, task, user=user)

    task = _begin_status(session, task, "reviewing", "review", "后台开始评审")
    background_tasks.add_task(job_review, task_id)
    return to_task_out(session, task, user=user)


@router.post("/{task_id}/optimize-prompt", response_model=TaskOut)
def optimize_prompt_task(
    task_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    wait: bool = Query(False),
    request: Request = None,
) -> TaskOut:
    with task_locks.hold(task_id):
        session.expire_all()
        return _optimize_prompt_task_locked(
            task_id,
            background_tasks,
            session,
            request=request,
            wait=wait,
        )


def _optimize_prompt_task_locked(
    task_id: int,
    background_tasks: BackgroundTasks,
    session: Session,
    *,
    request: Request | None,
    wait: bool,
) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    _ensure_task_prompt_accessible(session, task, request)
    user = _request_user(request)

    if task.status in _BUSY:
        return to_task_out(session, task, user=user)

    if _force_sync("optimize", wait):
        task = run_optimize_prompt(session, task_id, chat_fn=_chat_for("optimize"))
        return to_task_out(session, task, user=user)

    if task.status not in ("reviewed", "failed"):
        task = run_optimize_prompt(session, task_id, chat_fn=_chat_for("optimize"))
        return to_task_out(session, task, user=user)

    task = _begin_status(
        session, task, "optimizing", "optimize", "后台开始优化 Prompt"
    )
    background_tasks.add_task(job_optimize_prompt, task_id)
    return to_task_out(session, task, user=user)


@router.post("/{task_id}/apply-prompt", response_model=TaskOut)
def apply_prompt_task(
    task_id: int,
    body: ApplyPromptBody,
    session: Session = Depends(get_session),
    request: Request = None,
) -> TaskOut:
    with task_locks.hold(task_id):
        session.expire_all()
        return _apply_prompt_task_locked(task_id, body, session, request=request)


def _apply_prompt_task_locked(
    task_id: int,
    body: ApplyPromptBody,
    session: Session,
    *,
    request: Request | None,
) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    _ensure_task_prompt_accessible(session, task, request)
    user = _request_user(request)
    revision_row = session.get(PromptRevision, body.revision_id)
    if (
        revision_row is not None
        and revision_row.task_id == task.id
        and not _is_prompt_id_visible(session, revision_row.base_prompt_id, user)
    ):
        raise HTTPException(
            status_code=403,
            detail=_PRIVATE_TASK_PROMPT_FORBIDDEN_DETAIL,
        )
    if body.mode != "task_temp":
        raise HTTPException(
            status_code=422,
            detail="已禁止修改全局提示词，仅支持应用到当前任务（task_temp）",
        )

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
    return to_task_out(session, task, user=user)


@router.post("/{task_id}/regenerate", response_model=TaskOut)
def regenerate_task(
    task_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    wait: bool = Query(False),
    request: Request = None,
) -> TaskOut:
    with task_locks.hold(task_id):
        session.expire_all()
        return _regenerate_task_locked(
            task_id,
            background_tasks,
            session,
            request=request,
            wait=wait,
        )


def _regenerate_task_locked(
    task_id: int,
    background_tasks: BackgroundTasks,
    session: Session,
    *,
    request: Request | None,
    wait: bool,
) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    _ensure_task_prompt_accessible(session, task, request)
    user = _request_user(request)

    if task.status in _BUSY:
        return to_task_out(session, task, user=user)

    if _force_sync("generate", wait):
        _start_live_generate_stream(task, "开始重新生成测试用例")
        task = run_regenerate(session, task_id, chat_fn=_chat_for("generate"))
        return to_task_out(session, task, user=user)

    if task.status not in ("generated", "reviewed", "failed"):
        _start_live_generate_stream(task, "开始重新生成测试用例")
        task = run_regenerate(session, task_id, chat_fn=_chat_for("generate"))
        return to_task_out(session, task, user=user)

    task = _begin_status(
        session, task, "regenerating", "regenerate", "后台开始重新生成"
    )
    _start_live_generate_stream(task, "后台开始重新生成")
    background_tasks.add_task(job_regenerate, task_id)
    return to_task_out(session, task, user=user)


@router.get("/{task_id}/finalize-conflicts", response_model=FinalizeConflictsOut)
def finalize_conflicts_route(
    task_id: int,
    draft_id: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
) -> FinalizeConflictsOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if draft_id is None:
        draft = _latest_draft(session, task_id)
        if draft is None:
            raise HTTPException(status_code=400, detail="Task has no case draft")
    else:
        draft = session.get(CaseDraft, draft_id)
        if draft is None:
            raise HTTPException(status_code=400, detail="Draft not found")
        if draft.task_id != task_id:
            raise HTTPException(status_code=400, detail="Draft does not belong to task")
    try:
        conflicts = find_finalize_conflicts(session, task, draft)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FinalizeConflictsOut(conflicts=conflicts)


@router.post("/{task_id}/finalize", response_model=TaskOut)
def finalize_task_route(
    task_id: int,
    request: Request,
    body: FinalizeTaskBody | None = None,
    session: Session = Depends(get_session),
) -> TaskOut:
    with task_locks.hold(task_id):
        session.expire_all()
        return _finalize_task_locked(
            task_id,
            body,
            session,
            operator=_request_operator(request),
            user=current_user(request),
        )


def _finalize_task_locked(
    task_id: int,
    body: FinalizeTaskBody | None,
    session: Session,
    *,
    operator: str | None = None,
    user: Identity | None = None,
) -> TaskOut:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        task = finalize_task(
            session,
            task_id,
            draft_id=body.draft_id if body else None,
            overwrite_existing=body.overwrite_existing if body else False,
            operator=operator,
        )
    except CaseKeyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return to_task_out(session, task, user=user)


@router.get("/{task_id}/drafts", response_model=List[CaseDraftOut])
def list_drafts(
    task_id: int,
    session: Session = Depends(get_session),
    request: Request = None,
) -> list[CaseDraftOut]:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    user = _request_user(request)
    visibility_cache: dict[int, bool] = {}
    task_prompt_visible = _is_prompt_id_visible(
        session, task.prompt_template_id, user, visibility_cache
    )
    rows = session.exec(
        select(CaseDraft)
        .where(CaseDraft.task_id == task_id)
        .order_by(col(CaseDraft.version).desc())
    ).all()
    results: list[CaseDraftOut] = []
    for d in rows:
        points_with_cases = aggregate_draft_cases_by_points(session, task_id, d)
        results.append(
            CaseDraftOut(
                id=int(d.id),
                task_id=int(d.task_id),
                version=int(d.version),
                content_md=str(d.content_md),
                prompt_version_ref=_sanitize_prompt_ref(
                    session,
                    d.prompt_version_ref,
                    user,
                    task_prompt_visible=task_prompt_visible,
                    cache=visibility_cache,
                ),
                created_at=d.created_at,
                points_with_cases=points_with_cases,
            )
        )
    return results


@router.get("/{task_id}/citations", response_model=List[TaskCitationOut])
def list_citations(
    task_id: int, session: Session = Depends(get_session)
) -> list[TaskCitationOut]:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    rows = session.exec(
        select(TaskCitation)
        .where(TaskCitation.task_id == task_id)
        .order_by(col(TaskCitation.id).asc())
    ).all()
    out: list[TaskCitationOut] = []
    for r in rows:
        citation_type = getattr(r, "citation_type", None) or "wiki"
        target_available = citation_type == "external"
        legacy = not target_available
        out.append(
            _task_citation_out(
                r,
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
def list_events(
    task_id: int,
    session: Session = Depends(get_session),
    request: Request = None,
) -> list[TaskEventOut]:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    user = _request_user(request)
    visibility_cache: dict[int, bool] = {}
    task_prompt_visible = _is_prompt_id_visible(
        session, task.prompt_template_id, user, visibility_cache
    )
    rows = session.exec(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(col(TaskEvent.id).asc())
    ).all()
    try:
        space = resolve_space(session, task.wiki_space_id)
    except ValueError:
        space = None
    results: list[TaskEventOut] = []
    for row in rows:
        row_data = row.model_dump()
        row_data["detail_json"] = _sanitize_event_detail_json(
            session,
            row.detail_json,
            user,
            task_prompt_visible=task_prompt_visible,
            cache=visibility_cache,
        )
        results.append(
            TaskEventOut(
                **row_data,
                wiki_space_id=task.wiki_space_id,
                wiki_space_name=space.name if space else "",
            )
        )
    return results


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
    task_id: int,
    session: Session = Depends(get_session),
    request: Request = None,
) -> list[PromptRevision]:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    user = _request_user(request)
    visibility_cache: dict[int, bool] = {}
    if not _is_prompt_id_visible(
        session, task.prompt_template_id, user, visibility_cache
    ):
        return []
    rows = session.exec(
        select(PromptRevision)
        .where(PromptRevision.task_id == task_id)
        .order_by(col(PromptRevision.id).desc())
    ).all()
    return [
        row
        for row in rows
        if _is_prompt_id_visible(session, row.base_prompt_id, user, visibility_cache)
    ]
