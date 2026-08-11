from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import get_session
from app.models.entities import Document, IngestJob, WikiPageRow, WikiReviewItem, WikiSpace
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
def list_wiki_spaces(session: Session = Depends(get_session)) -> list[WikiSpaceOut]:
    # A fresh/legacy database must always expose the compatibility namespace.
    default = get_default_space(session, create=True)
    if default is not None and default.id is None:
        session.commit()
        session.refresh(default)
    rows = session.exec(
        select(WikiSpace).order_by(WikiSpace.status, WikiSpace.name, WikiSpace.id)
    ).all()
    return [_out(session, row) for row in rows]


@router.post("", response_model=WikiSpaceOut, status_code=status.HTTP_201_CREATED)
def create_wiki_space(
    body: WikiSpaceCreate,
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
    row = WikiSpace(
        name=body.name,
        slug=slug,
        description=body.description or "",
        status=ACTIVE_SPACE_STATUS,
    )
    session.add(row)
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
    session: Session = Depends(get_session),
) -> WikiSpaceOut:
    row = _get(session, space_id)
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
    session: Session = Depends(get_session),
) -> WikiSpaceOut:
    row = _get(session, space_id)
    return _set_status(session, row, ARCHIVED_SPACE_STATUS)


@router.patch("/{space_id}/status", response_model=WikiSpaceOut)
def update_wiki_space_status(
    space_id: int,
    body: WikiSpaceStatusUpdate,
    session: Session = Depends(get_session),
) -> WikiSpaceOut:
    return _set_status(session, _get(session, space_id), body.status)
