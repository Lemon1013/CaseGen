"""Protected current-state test-case asset API."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import List
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.db import get_session
from app.models.entities import Requirement, TestCase, TestCaseOperationLog
from app.schemas.tasks import (
    TestCaseCreate,
    TestCaseOperationLogOut,
    TestCaseOut,
    TestCaseUpdate,
)
from app.services.case_management import (
    CaseDraftParseError,
    append_case_log,
    normalize_case_key,
    stable_cases,
)

router = APIRouter(prefix="/api/cases", tags=["cases"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return str(getattr(user, "username", None) or "system")[:128]


def _case_out(row: TestCase) -> TestCaseOut:
    return TestCaseOut(
        id=int(row.id or 0),
        requirement_id=int(row.requirement_id),
        case_key=row.case_key,
        source_case_key=row.source_case_key,
        title=row.title,
        content_md=row.content_md,
        content=row.content_md,
        status=row.status,
        revision=int(row.revision),
        source_task_id=row.source_task_id,
        source_draft_id=row.source_draft_id,
        archived_at=row.archived_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _get_case(session: Session, case_id: int) -> TestCase:
    row = session.get(TestCase, case_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    return row


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


@router.get("", response_model=List[TestCaseOut])
def list_cases(
    requirement_id: int | None = Query(default=None, ge=1),
    include_archived: bool = Query(default=False),
    keyword: str | None = Query(default=None, max_length=200),
    status: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[TestCaseOut]:
    statement = select(TestCase)
    if requirement_id is not None:
        statement = statement.where(TestCase.requirement_id == requirement_id)
    if status not in {None, "", "active", "archived"}:
        raise HTTPException(status_code=422, detail="status must be active or archived")
    if status:
        statement = statement.where(TestCase.status == status)
    elif not include_archived:
        statement = statement.where(TestCase.status != "archived")
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
    rows = session.exec(
        statement.order_by(col(TestCase.requirement_id).asc(), col(TestCase.case_key).asc(), col(TestCase.id).asc())
    ).all()
    return [_case_out(row) for row in rows]


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
    session: Session = Depends(get_session),
) -> TestCaseOut:
    content = body.content_md if body.content_md is not None else body.content
    if not content or not content.strip():
        raise HTTPException(status_code=422, detail="content_md is required")
    if session.get(Requirement, body.requirement_id) is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
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
    case = TestCase(
        requirement_id=body.requirement_id,
        case_key=case_key,
        title=title,
        content_md=content,
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
    return _case_out(case)


@router.get("/{case_id}", response_model=TestCaseOut)
def get_case(case_id: int, session: Session = Depends(get_session)) -> TestCaseOut:
    return _case_out(_get_case(session, case_id))


@router.patch("/{case_id}", response_model=TestCaseOut)
def update_case(
    case_id: int,
    body: TestCaseUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> TestCaseOut:
    row = _get_case(session, case_id)
    expected_revision = body.expected_revision if body.expected_revision is not None else body.revision
    _check_revision(row, expected_revision, body.expected_updated_at)
    content = body.content_md if body.content_md is not None else body.content
    if content is not None and not content.strip():
        raise HTTPException(status_code=422, detail="content_md cannot be empty")
    before = row.content_md
    title_changed = body.title is not None and body.title != row.title
    content_changed = content is not None and content != row.content_md
    if not title_changed and not content_changed:
        return _case_out(row)
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="title cannot be empty")
        row.title = title
    if content is not None:
        row.content_md = content
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
    )
    session.commit()
    session.refresh(row)
    return _case_out(row)


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
        return _case_out(row)
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
    return _case_out(row)


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
