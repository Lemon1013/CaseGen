"""Protected current-state test-case asset API."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import List
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.db import get_session
from app.models.entities import CaseDraft, Requirement, TestCase, TestCaseOperationLog
from app.schemas.tasks import (
    TestCaseCreate,
    TestCaseOperationLogOut,
    TestCaseOut,
    TestCaseSubmitterOut,
    TestCaseUpdate,
)
from app.services.case_management import (
    CaseDraftParseError,
    append_case_log,
    normalize_case_key,
    stable_cases,
)
from app.services.permissions import (
    load_user_profiles,
    request_user_identity,
    resolve_submitter_info,
)

router = APIRouter(prefix="/api/cases", tags=["cases"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return str(getattr(user, "username", None) or "system")[:128]


def _case_out_from_maps(
    row: TestCase,
    draft_map: dict[int, CaseDraft],
    user_profiles: dict[int, tuple[str, str]],
) -> TestCaseOut:
    source_draft = (
        draft_map.get(int(row.source_draft_id))
        if row.source_draft_id is not None
        else None
    )
    created_by_username, created_by_display_name = resolve_submitter_info(
        getattr(row, "created_by_user_id", None),
        getattr(row, "created_by_username", None),
        user_profiles,
    )
    return TestCaseOut(
        id=int(row.id or 0),
        requirement_id=int(row.requirement_id),
        case_key=row.case_key,
        source_case_key=row.source_case_key,
        title=row.title,
        content_md=row.content_md,
        content=row.content_md,
        status=row.status,
        priority=getattr(row, "priority", None) or "P1",
        revision=int(row.revision),
        source_task_id=row.source_task_id,
        source_draft_id=row.source_draft_id,
        source_draft_version=(
            int(source_draft.version)
            if source_draft is not None and source_draft.task_id == row.source_task_id
            else None
        ),
        created_by_user_id=getattr(row, "created_by_user_id", None),
        created_by_username=created_by_username,
        created_by_display_name=created_by_display_name,
        archived_at=row.archived_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _cases_out(session: Session, rows: list[TestCase]) -> list[TestCaseOut]:
    if not rows:
        return []
    draft_ids = list({int(row.source_draft_id) for row in rows if row.source_draft_id is not None})
    draft_map: dict[int, CaseDraft] = (
        {
            int(draft.id): draft
            for draft in session.exec(
                select(CaseDraft).where(col(CaseDraft.id).in_(draft_ids))
            ).all()
            if draft.id is not None
        }
        if draft_ids
        else {}
    )
    user_profiles = load_user_profiles(
        session,
        [getattr(row, "created_by_user_id", None) for row in rows],
    )
    return [_case_out_from_maps(row, draft_map, user_profiles) for row in rows]


def _case_out(session: Session, row: TestCase) -> TestCaseOut:
    return _cases_out(session, [row])[0]


def _get_case(session: Session, case_id: int) -> TestCase:
    row = session.get(TestCase, case_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    return row


def _check_project(session: Session, row: TestCase, project_id: int | None) -> None:
    if project_id is None:
        return
    requirement = session.get(Requirement, row.requirement_id)
    if requirement is None or requirement.project_id != project_id:
        raise HTTPException(status_code=404, detail="Test case not found in project")


def _check_revision(row: TestCase, expected_revision: int | None, expected_updated_at: datetime | None) -> None:
    if expected_revision is not None and int(row.revision) != int(expected_revision):
        raise HTTPException(
            status_code=409,
            detail=f"Case has changed; expected revision {expected_revision}, current revision {row.revision}",
        )
    if expected_updated_at is not None:
        expected = expected_updated_at.replace(tzinfo=None)
        current = row.updated_at.replace(tzinfo=None)
        if expected != current:
            raise HTTPException(status_code=409, detail="Case has changed; refresh before editing")


def _parse_ids(raw: str | None) -> list[int] | None:
    if raw is None or not raw.strip():
        return None
    values: list[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = int(item)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="ids must be comma-separated integers") from exc
        if value < 1:
            raise HTTPException(status_code=422, detail="ids must be positive integers")
        values.append(value)
    return list(dict.fromkeys(values))


def _load_export_cases(
    session: Session,
    *,
    ids: list[int] | None,
    requirement_id: int | None,
) -> list[TestCase]:
    statement = select(TestCase).where(TestCase.status == "active")
    if ids is not None:
        if not ids:
            return []
        statement = statement.where(TestCase.id.in_(ids))
    if requirement_id is not None:
        statement = statement.where(TestCase.requirement_id == requirement_id)
    rows = session.exec(statement).all()
    return stable_cases(rows)


def _markdown_export(session: Session, rows: list[TestCase]) -> str:
    grouped: dict[int, list[TestCase]] = {}
    for row in rows:
        grouped.setdefault(int(row.requirement_id), []).append(row)

    chunks: list[str] = ["# CaseGen Test Cases", ""]
    for requirement_id in sorted(grouped):
        requirement = session.get(Requirement, requirement_id)
        requirement_title = requirement.title.strip() if requirement and requirement.title else f"需求 #{requirement_id}"
        requirement_description = requirement.description.strip() if requirement else ""
        chunks.append(f"# 需求：{requirement_title}")
        if requirement_description:
            chunks.append(requirement_description)
        chunks.append("")
        for row in grouped[requirement_id]:
            content = (row.content_md or "").strip()
            key_pattern = re.compile(rf"(?im)^##[ \t]+{re.escape(row.case_key)}(?:[ \t]|$)")
            if not key_pattern.search(content):
                title = (row.title or row.case_key or "Test Case").strip()
                chunks.append(f"## {row.case_key} - {title}")
                chunks.append(content)
            else:
                # Imported Markdown already carries its ``## TC-xxx`` heading;
                # retain it verbatim instead of generating a duplicate.
                chunks.append(content)
            chunks.append("")
    return "\n".join(chunks).rstrip() + "\n"


def _export_response(content: str, filename: str = "casegen-cases.md") -> Response:
    # The filename is backend-owned and quoted through RFC 5987, so no user
    # input can inject a response header or path separator.
    safe_name = filename.replace("\r", "").replace("\n", "")
    ascii_name = safe_name.encode("ascii", "ignore").decode("ascii") or "casegen-cases.md"
    disposition = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(safe_name)}'
    return Response(
        content=content.encode("utf-8"),
        media_type="text/markdown",
        headers={
            "Content-Disposition": disposition,
            "Content-Type": "text/markdown; charset=utf-8",
            "Cache-Control": "no-store",
        },
    )


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ALLOWED_SORTS = {"created_desc", "created_asc", "updated_desc", "case_key_asc"}


def _parse_datetime_bound(raw: str, *, is_end: bool) -> tuple[datetime, bool]:
    text = raw.strip()
    if _DATE_ONLY_RE.match(text):
        try:
            day = date.fromisoformat(text)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid date format; expected YYYY-MM-DD") from exc
        base = datetime(day.year, day.month, day.day)
        if is_end:
            return base + timedelta(days=1), True
        return base, False
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid datetime format") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed, False


@router.get("", response_model=List[TestCaseOut])
def list_cases(
    project_id: int | None = Query(default=None, ge=1),
    requirement_id: int | None = Query(default=None, ge=1),
    include_archived: bool = Query(default=False),
    keyword: str | None = Query(default=None, max_length=200),
    status: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    created_by: str | None = Query(default=None, max_length=128),
    source_type: str | None = Query(default=None),
    created_from: str | None = Query(default=None, max_length=64),
    created_to: str | None = Query(default=None, max_length=64),
    sort: str = Query(default="created_desc"),
    session: Session = Depends(get_session),
) -> list[TestCaseOut]:
    statement = select(TestCase)
    if requirement_id is not None:
        statement = statement.where(TestCase.requirement_id == requirement_id)
    if project_id is not None:
        statement = statement.where(TestCase.requirement_id.in_(select(Requirement.id).where(Requirement.project_id == project_id)))
    if status not in {None, "", "active", "archived"}:
        raise HTTPException(status_code=422, detail="status must be active or archived")
    if status:
        statement = statement.where(TestCase.status == status)
    elif not include_archived:
        statement = statement.where(TestCase.status != "archived")
    if priority not in {None, "", "P0", "P1", "P2"}:
        raise HTTPException(status_code=422, detail="priority must be P0, P1 or P2")
    if priority:
        statement = statement.where(TestCase.priority == priority)
    if created_by and created_by.strip():
        statement = statement.where(
            func.lower(func.trim(TestCase.created_by_username)) == created_by.strip().casefold()
        )
    if source_type not in {None, "", "task", "direct"}:
        raise HTTPException(status_code=422, detail="source_type must be task or direct")
    if source_type == "task":
        statement = statement.where(col(TestCase.source_task_id).is_not(None))
    elif source_type == "direct":
        statement = statement.where(col(TestCase.source_task_id).is_(None))
    if created_from and created_from.strip():
        start_dt, _ = _parse_datetime_bound(created_from, is_end=False)
        statement = statement.where(TestCase.created_at >= start_dt)
    if created_to and created_to.strip():
        end_dt, exclusive = _parse_datetime_bound(created_to, is_end=True)
        if exclusive:
            statement = statement.where(TestCase.created_at < end_dt)
        else:
            statement = statement.where(TestCase.created_at <= end_dt)
    if sort not in _ALLOWED_SORTS:
        raise HTTPException(
            status_code=422,
            detail="sort must be created_desc, created_asc, updated_desc or case_key_asc",
        )
    if keyword and keyword.strip():
        pattern = f"%{keyword.strip()}%"
        statement = statement.join(Requirement, TestCase.requirement_id == Requirement.id).where(
            or_(
                TestCase.case_key.ilike(pattern),
                TestCase.title.ilike(pattern),
                TestCase.content_md.ilike(pattern),
                Requirement.title.ilike(pattern),
            )
        )
    if sort == "created_asc":
        order_cols = (col(TestCase.created_at).asc(), col(TestCase.id).asc())
    elif sort == "updated_desc":
        order_cols = (col(TestCase.updated_at).desc(), col(TestCase.id).desc())
    elif sort == "case_key_asc":
        order_cols = (col(TestCase.requirement_id).asc(), col(TestCase.case_key).asc(), col(TestCase.id).asc())
    else:
        order_cols = (col(TestCase.created_at).desc(), col(TestCase.id).desc())
    rows = list(session.exec(statement.order_by(*order_cols)).all())
    return _cases_out(session, rows)


@router.get("/submitters", response_model=List[TestCaseSubmitterOut])
def list_case_submitters(
    project_id: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
) -> list[TestCaseSubmitterOut]:
    statement = select(TestCase.created_by_user_id, TestCase.created_by_username).where(
        col(TestCase.created_by_username).is_not(None),
        func.trim(TestCase.created_by_username) != "",
    )
    if project_id is not None:
        statement = statement.where(
            TestCase.requirement_id.in_(select(Requirement.id).where(Requirement.project_id == project_id))
        )
    raw_pairs = session.exec(statement.distinct()).all()
    user_profiles = load_user_profiles(session, [uid for uid, _ in raw_pairs])
    seen: dict[str, TestCaseSubmitterOut] = {}
    for uid, uname in raw_pairs:
        resolved_username, resolved_display = resolve_submitter_info(uid, uname, user_profiles)
        if not resolved_username:
            continue
        key = resolved_username.casefold()
        if key not in seen:
            seen[key] = TestCaseSubmitterOut(
                user_id=uid,
                username=resolved_username,
                display_name=resolved_display,
            )
    return sorted(seen.values(), key=lambda item: item.username.casefold())


@router.get("/export")
def export_cases(
    request: Request,
    ids: str | None = Query(default=None),
    requirement_id: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
) -> Response:
    rows = _load_export_cases(session, ids=_parse_ids(ids), requirement_id=requirement_id)
    if ids and not rows:
        raise HTTPException(status_code=409, detail="No active test cases selected for export")
    for row in rows:
        append_case_log(
            session,
            row,
            "export",
            before=row.content_md,
            after=row.content_md,
            reason="Markdown export",
            operator=_operator(request),
            source_task_id=row.source_task_id,
            source_draft_id=row.source_draft_id,
            source_case_key=row.source_case_key,
        )
    if rows:
        session.commit()
    return _export_response(_markdown_export(session, rows))


@router.get("/{case_id}/export")
def export_case(case_id: int, request: Request, session: Session = Depends(get_session)) -> Response:
    row = _get_case(session, case_id)
    if row.status != "active":
        raise HTTPException(status_code=409, detail="Archived test cases cannot be exported")
    append_case_log(
        session,
        row,
        "export",
        before=row.content_md,
        after=row.content_md,
        reason="Markdown export",
        operator=_operator(request),
        source_task_id=row.source_task_id,
        source_draft_id=row.source_draft_id,
        source_case_key=row.source_case_key,
    )
    session.commit()
    filename = f"case-{row.case_key}.md" if row.case_key else "casegen-case.md"
    filename = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in filename)
    return _export_response(_markdown_export(session, [row]), filename)


@router.post("", response_model=TestCaseOut, status_code=201)
def create_case(
    body: TestCaseCreate,
    request: Request,
    project_id: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
) -> TestCaseOut:
    content = body.content_md if body.content_md is not None else body.content
    if not content or not content.strip():
        raise HTTPException(status_code=422, detail="content_md is required")
    requirement = session.get(Requirement, body.requirement_id)
    if requirement is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    if requirement.project_id is not None:
        from app.services.projects import require_request_project
        require_request_project(request, requirement.project_id)
    if project_id is not None and requirement.project_id != project_id:
        raise HTTPException(status_code=422, detail="Requirement belongs to another project")
    try:
        case_key = normalize_case_key(body.case_key)
    except CaseDraftParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    title = (body.title if body.title is not None else case_key).strip()
    if not title:
        raise HTTPException(status_code=422, detail="title cannot be empty")
    duplicate = session.exec(
        select(TestCase)
        .where(
            TestCase.requirement_id == body.requirement_id,
            func.lower(func.trim(TestCase.case_key)) == case_key.casefold(),
        )
    ).first()
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Case key {case_key} already exists for this requirement",
        )
    created_by_user_id, created_by_username = request_user_identity(request)
    case = TestCase(
        requirement_id=body.requirement_id,
        case_key=case_key,
        title=title,
        content_md=content,
        priority=body.priority,
        created_by_user_id=created_by_user_id,
        created_by_username=created_by_username,
        status="active",
        revision=1,
    )
    session.add(case)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Case key already exists for this requirement") from exc
    append_case_log(session, case, "create", after=case.content_md, operator=_operator(request))
    session.commit()
    session.refresh(case)
    return _case_out(session, case)


@router.get("/{case_id}", response_model=TestCaseOut)
def get_case(case_id: int, project_id: int | None = Query(default=None, ge=1), session: Session = Depends(get_session)) -> TestCaseOut:
    row = _get_case(session, case_id); _check_project(session, row, project_id)
    return _case_out(session, row)


@router.patch("/{case_id}", response_model=TestCaseOut)
def update_case(
    case_id: int,
    body: TestCaseUpdate,
    request: Request,
    project_id: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
) -> TestCaseOut:
    row = _get_case(session, case_id)
    _check_project(session, row, project_id)
    expected_revision = body.expected_revision if body.expected_revision is not None else body.revision
    _check_revision(row, expected_revision, body.expected_updated_at)
    content = body.content_md if body.content_md is not None else body.content
    if content is not None and not content.strip():
        raise HTTPException(status_code=422, detail="content_md cannot be empty")
    before = row.content_md
    title_changed = body.title is not None and body.title != row.title
    content_changed = content is not None and content != row.content_md
    priority_changed = body.priority is not None and body.priority != (getattr(row, "priority", None) or "P1")
    if not title_changed and not content_changed and not priority_changed:
        return _case_out(session, row)
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="title cannot be empty")
        row.title = title
    if content is not None:
        row.content_md = content
    if body.priority is not None:
        row.priority = body.priority
    row.revision += 1
    row.updated_at = _utcnow()
    session.add(row)
    append_case_log(
        session,
        row,
        "edit",
        before=before,
        after=row.content_md,
        reason=body.reason,
        operator=_operator(request),
        source_task_id=row.source_task_id,
        source_draft_id=row.source_draft_id,
        source_case_key=row.source_case_key,
        title_changed=title_changed,
        changed_fields={"priority"} if priority_changed else None,
    )
    session.commit()
    session.refresh(row)
    return _case_out(session, row)


@router.get("/{case_id}/logs", response_model=List[TestCaseOperationLogOut])
def list_case_logs(case_id: int, session: Session = Depends(get_session)) -> list[TestCaseOperationLogOut]:
    _get_case(session, case_id)
    rows = session.exec(
        select(TestCaseOperationLog)
        .where(TestCaseOperationLog.test_case_id == case_id)
        .order_by(col(TestCaseOperationLog.created_at).asc(), col(TestCaseOperationLog.id).asc())
    ).all()
    output: list[TestCaseOperationLogOut] = []
    for row in rows:
        try:
            changed_fields = json.loads(row.changed_fields_json or "[]")
        except (TypeError, ValueError):
            changed_fields = []
        if not isinstance(changed_fields, list):
            changed_fields = []
        output.append(
            TestCaseOperationLogOut(
            id=int(row.id or 0),
            test_case_id=row.test_case_id,
            operation=row.operation,
            changed_fields=[str(item) for item in changed_fields],
            before_hash=row.before_hash,
            after_hash=row.after_hash,
            before_length=row.before_length,
            after_length=row.after_length,
            added_lines=row.added_lines,
            deleted_lines=row.deleted_lines,
            title_changed=row.title_changed,
            diff_summary=row.diff_summary,
            reason=row.reason,
            operator=row.operator,
            source_task_id=row.source_task_id,
            source_draft_id=row.source_draft_id,
            source_case_key=row.source_case_key,
            created_at=row.created_at,
            )
        )
    return output


def _set_archive(
    case_id: int,
    request: Request,
    session: Session,
    *,
    archived: bool,
    expected_revision: int | None,
) -> TestCaseOut:
    row = _get_case(session, case_id)
    _check_revision(row, expected_revision, None)
    target = "archived" if archived else "active"
    if row.status == target:
        return _case_out(session, row)
    before = row.content_md
    row.status = target
    row.archived_at = _utcnow() if archived else None
    row.revision += 1
    row.updated_at = _utcnow()
    session.add(row)
    append_case_log(
        session,
        row,
        "archive" if archived else "restore",
        before=before,
        after=row.content_md,
        operator=_operator(request),
        source_task_id=row.source_task_id,
        source_draft_id=row.source_draft_id,
        source_case_key=row.source_case_key,
        changed_fields={"status"},
    )
    session.commit()
    session.refresh(row)
    return _case_out(session, row)


@router.post("/{case_id}/archive", response_model=TestCaseOut)
def archive_case(
    case_id: int,
    request: Request,
    expected_revision: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
) -> TestCaseOut:
    return _set_archive(case_id, request, session, archived=True, expected_revision=expected_revision)


@router.post("/{case_id}/restore", response_model=TestCaseOut)
@router.post("/{case_id}/unarchive", response_model=TestCaseOut)
def restore_case(
    case_id: int,
    request: Request,
    expected_revision: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
) -> TestCaseOut:
    return _set_archive(case_id, request, session, archived=False, expected_revision=expected_revision)
