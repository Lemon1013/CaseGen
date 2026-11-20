"""Domain helpers for compatibility Wiki Space shell resolution."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import or_
from sqlmodel import Session, select

from app.models.entities import Project, WikiSpace

DEFAULT_SPACE_SLUG = "default"
DEFAULT_SPACE_NAME = "默认空间"
ACTIVE_SPACE_STATUS = "active"
ARCHIVED_SPACE_STATUS = "archived"
SPACE_STATUSES = frozenset({ACTIVE_SPACE_STATUS, ARCHIVED_SPACE_STATUS})
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_NAME_RE = re.compile(r"[^a-z0-9]+")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_space_slug(slug: str) -> str:
    """Normalize and validate a user-facing slug, rejecting path syntax."""

    value = str(slug or "").strip().lower().replace("_", "-")
    value = re.sub(r"-+", "-", value).strip("-")
    if not value or not _SLUG_RE.fullmatch(value) or value in {".", "..", "spaces"}:
        raise ValueError(
            "slug must contain only lowercase letters, numbers and single hyphens"
        )
    return value


def slug_from_name(name: str) -> str:
    value = _SAFE_NAME_RE.sub("-", str(name or "").strip().lower()).strip("-")
    if not value:
        value = f"space-{uuid4().hex[:10]}"
    return normalize_space_slug(value[:64].strip("-"))


def validate_space_status(status: str) -> str:
    value = str(status or "").strip().lower()
    if value not in SPACE_STATUSES:
        raise ValueError(f"unsupported Wiki space status: {status!r}")
    return value


def get_default_space(session: Session, *, create: bool = True) -> WikiSpace | None:
    row = session.exec(
        select(WikiSpace).where(WikiSpace.slug == DEFAULT_SPACE_SLUG)
    ).first()
    if row is not None or not create:
        return row
    project = session.exec(select(Project).where(Project.slug == DEFAULT_SPACE_SLUG)).first()
    if project is None:
        project = Project(name=DEFAULT_SPACE_NAME, slug=DEFAULT_SPACE_SLUG, description="兼容旧数据的默认项目")
        session.add(project)
        session.flush()
    row = WikiSpace(
        name=DEFAULT_SPACE_NAME,
        slug=DEFAULT_SPACE_SLUG,
        description="由系统迁移和兼容旧 Wiki 数据使用的默认空间",
        status=ACTIVE_SPACE_STATUS,
        scope="project",
        project_id=project.id,
    )
    session.add(row)
    session.flush()
    project.default_wiki_space_id = row.id
    session.add(project)
    return row


def resolve_space(
    session: Session,
    space_id: int | None = None,
    *,
    create_default: bool = True,
    allow_archived: bool = True,
    for_write: bool = False,
) -> WikiSpace:
    """Resolve an explicit id or the compatibility default space.

    This helper is intentionally called by every compatibility path.  It does
    not return a global collection and therefore cannot silently broaden a
    retrieval query.
    """

    row = (
        get_default_space(session, create=create_default)
        if space_id is None
        else session.get(WikiSpace, int(space_id))
    )
    if row is None:
        raise ValueError("Wiki space not found")
    if row.status not in SPACE_STATUSES:
        raise ValueError(f"invalid Wiki space status: {row.status!r}")
    if not allow_archived and row.status == ARCHIVED_SPACE_STATUS:
        raise ValueError("Archived Wiki spaces are read-only")
    if for_write and row.status != ACTIVE_SPACE_STATUS:
        raise ValueError("Archived Wiki spaces are read-only")
    return row


def resolve_space_id(session: Session, space_id: int | None = None, **kwargs: Any) -> int:
    row = resolve_space(session, space_id, **kwargs)
    if row.id is None:
        raise ValueError("Wiki space has no id")
    return int(row.id)


def space_scope_clause(session: Session, column: Any, space_id: int):
    """Return an isolation predicate, including NULL legacy rows only in default."""

    default = get_default_space(session, create=True)
    if default is not None and default.id is not None and int(default.id) == int(space_id):
        return or_(column == int(space_id), column.is_(None))
    return column == int(space_id)


def space_statistics(session: Session, space: WikiSpace) -> dict[str, Any]:
    return {
        "document_count": 0,
        "page_count": 0,
        "pending_review_count": 0,
        "last_updated_at": space.updated_at,
    }


def space_to_dict(session: Session, space: WikiSpace) -> dict[str, Any]:
    return {
        "id": int(space.id or 0),
        "name": space.name,
        "slug": space.slug,
        "description": space.description or "",
        "status": space.status,
        "scope": space.scope,
        "project_id": space.project_id,
        "namespace": space.namespace,
        "created_at": space.created_at,
        "updated_at": space.updated_at,
        **space_statistics(session, space),
    }


__all__ = [
    "ACTIVE_SPACE_STATUS",
    "ARCHIVED_SPACE_STATUS",
    "DEFAULT_SPACE_NAME",
    "DEFAULT_SPACE_SLUG",
    "SPACE_STATUSES",
    "get_default_space",
    "normalize_space_slug",
    "resolve_space",
    "resolve_space_id",
    "space_scope_clause",
    "slug_from_name",
    "space_statistics",
    "space_to_dict",
    "validate_space_status",
]
