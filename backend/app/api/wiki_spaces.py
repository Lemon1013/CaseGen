from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import get_session
from app import config
from app.models.entities import Document, IngestJob, Project, WikiPageRow, WikiReviewItem, WikiSpace
from app.schemas.wiki_spaces import (
    WikiSpaceCreate,
    WikiSpaceOut,
    WikiSpaceStatusUpdate,
    WikiSpaceUpdate,
)
from app.services.wiki_spaces import (
    ACTIVE_SPACE_STATUS,
    ARCHIVED_SPACE_STATUS,
    DEFAULT_SPACE_SLUG,
    ensure_space_dirs,
    get_default_space,
    normalize_space_slug,
    slug_from_name,
    space_to_dict,
)

router = APIRouter(prefix="/api/wiki-spaces", tags=["wiki-spaces"])


def _require_admin_for_shared(request: Request) -> None:
    if not config.AUTH_ENABLED:
        return
    user = getattr(request.state, "user", None)
    if user is None or getattr(user, "role", "") != "admin":
        raise HTTPException(status_code=403, detail="Shared Wiki spaces require an administrator")


def _out(session: Session, row: WikiSpace) -> WikiSpaceOut:
    return WikiSpaceOut.model_validate(space_to_dict(session, row))


def _get(session: Session, space_id: int) -> WikiSpace:
    row = session.get(WikiSpace, space_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Wiki space not found")
    return row


def _set_status(
    session: Session,
    row: WikiSpace,
    next_status: str,
) -> WikiSpaceOut:
    if next_status == row.status:
        return _out(session, row)
    if next_status == ARCHIVED_SPACE_STATUS:
        if row.slug == DEFAULT_SPACE_SLUG:
            raise HTTPException(
                status_code=409,
                detail="The default Wiki space cannot be archived",
            )
        active_job = session.exec(
            select(IngestJob)
            .where(
                IngestJob.space_id == row.id,
                IngestJob.status.in_(["queued", "running"]),
            )
            .order_by(IngestJob.id.desc())
        ).first()
        if active_job is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Wiki space has active ingest job #{active_job.id}; "
                    "cancel it before archiving"
                ),
            )
        if row.scope == "project" and row.project_id is not None:
            project = session.get(Project, row.project_id)
            if project is not None and project.status == "active" and project.default_wiki_space_id == row.id:
                raise HTTPException(
                    status_code=409,
                    detail="An active project's default Wiki space cannot be archived",
                )
    elif next_status == ACTIVE_SPACE_STATUS:
        ensure_space_dirs(row)
    else:  # The request schema should reject this; keep the service boundary strict.
        raise HTTPException(status_code=422, detail="Unsupported Wiki space status")

    row.status = next_status
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _out(session, row)


@router.get("", response_model=list[WikiSpaceOut])
def list_wiki_spaces(project_id: int | None = Query(default=None, ge=1), include_available_shared: bool = False, session: Session = Depends(get_session)) -> list[WikiSpaceOut]:
    # A fresh/legacy database must always expose the compatibility namespace.
    default = get_default_space(session, create=True)
    if default is not None and default.id is None:
        session.commit()
        session.refresh(default)
    statement = select(WikiSpace)
    if project_id is not None:
        from app.services.projects import project_space_ids
        try:
            ids = project_space_ids(session, project_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        statement = statement.where((WikiSpace.id.in_(ids)) | ((WikiSpace.scope == "shared") if include_available_shared else (WikiSpace.id == -1)))
    rows = session.exec(statement.order_by(WikiSpace.status, WikiSpace.name, WikiSpace.id)).all()
    return [_out(session, row) for row in rows]


@router.post("", response_model=WikiSpaceOut, status_code=status.HTTP_201_CREATED)
def create_wiki_space(
    body: WikiSpaceCreate,
    request: Request,
    current_project_id: int | None = Query(default=None, alias="project_id", ge=1),
    session: Session = Depends(get_session),
) -> WikiSpaceOut:
    slug = body.slug or slug_from_name(body.name)
    try:
        slug = normalize_space_slug(slug)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if slug == DEFAULT_SPACE_SLUG:
        raise HTTPException(status_code=409, detail="The default Wiki space is reserved")
    existing = session.exec(select(WikiSpace).where(WikiSpace.slug == slug)).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Wiki space slug already exists")
    if body.scope not in {"shared", "project"}:
        raise HTTPException(status_code=422, detail="scope must be shared or project")
    requested_project_id = body.project_id or (current_project_id if body.scope == "project" else None)
    if body.project_id is not None:
        from app.services.projects import require_request_project
        require_request_project(request, body.project_id)
    project = session.get(Project, requested_project_id) if requested_project_id else None
    try:
        namespace = normalize_space_slug(body.namespace) if body.namespace else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.scope == "shared":
        _require_admin_for_shared(request)
        if body.project_id is not None or namespace is None:
            raise HTTPException(status_code=422, detail="Shared Wiki spaces require namespace and no project_id")
    elif requested_project_id is not None and project is None:
        raise HTTPException(status_code=422, detail="Project not found")
    elif requested_project_id is None:
        project = Project(name=body.name, slug=slug, description=body.description or "")
        session.add(project)
        session.flush()
    row = WikiSpace(
        name=body.name,
        slug=slug,
        description=body.description or "",
        status=ACTIVE_SPACE_STATUS,
        scope=body.scope,
        project_id=project.id if project is not None else None,
        namespace=namespace,
    )
    session.add(row)
    session.flush()
    if project is not None and project.default_wiki_space_id is None:
        project.default_wiki_space_id = row.id
        session.add(project)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Wiki space slug already exists") from exc
    session.refresh(row)
    ensure_space_dirs(row)
    return _out(session, row)


@router.get("/{space_id}", response_model=WikiSpaceOut)
def get_wiki_space(space_id: int, session: Session = Depends(get_session)) -> WikiSpaceOut:
    return _out(session, _get(session, space_id))


@router.put("/{space_id}", response_model=WikiSpaceOut)
def update_wiki_space(
    space_id: int,
    body: WikiSpaceUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> WikiSpaceOut:
    row = _get(session, space_id)
    if row.scope == "shared":
        _require_admin_for_shared(request)
    if row.status == ARCHIVED_SPACE_STATUS:
        raise HTTPException(status_code=409, detail="Archived Wiki spaces are read-only")
    if body.name is not None:
        row.name = body.name
    if body.description is not None:
        row.description = body.description
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _out(session, row)


@router.post("/{space_id}/archive", response_model=WikiSpaceOut)
def archive_wiki_space(
    space_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> WikiSpaceOut:
    row = _get(session, space_id)
    if row.scope == "shared":
        _require_admin_for_shared(request)
    return _set_status(session, row, ARCHIVED_SPACE_STATUS)


@router.patch("/{space_id}/status", response_model=WikiSpaceOut)
def update_wiki_space_status(
    space_id: int,
    body: WikiSpaceStatusUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> WikiSpaceOut:
    row = _get(session, space_id)
    if row.scope == "shared":
        _require_admin_for_shared(request)
    return _set_status(session, row, body.status)
