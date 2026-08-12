"""Domain helpers for Wiki Space resolution and filesystem boundaries."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_
from sqlmodel import Session, select

from app import config
from app.models.entities import (
    Document,
    WikiPageRow,
    WikiReviewItem,
    WikiSpace,
)

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
    row = WikiSpace(
        name=DEFAULT_SPACE_NAME,
        slug=DEFAULT_SPACE_SLUG,
        description="由系统迁移和兼容旧 Wiki 数据使用的默认空间",
        status=ACTIVE_SPACE_STATUS,
    )
    session.add(row)
    session.flush()
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


def space_root(space: WikiSpace | int | str) -> Path:
    """Return a validated space directory below ``data/wiki/spaces``."""

    slug = space.slug if isinstance(space, WikiSpace) else str(space)
    slug = normalize_space_slug(slug)
    root = (Path(config.WIKI_DIR) / "spaces").resolve()
    candidate = (root / slug).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Wiki space path escapes configured Wiki root") from exc
    return candidate


def ensure_space_dirs(space: WikiSpace) -> Path:
    root = space_root(space)
    for path in (root, root / "pages", root / "sources", root / "rules", root / "entities", root / "scenarios", root / "regressions", root / "synthesis"):
        path.mkdir(parents=True, exist_ok=True)
    for filename, heading in (("index.md", "# Wiki Index"), ("overview.md", "# Wiki Overview"), ("log.md", "# Wiki Log")):
        path = root / filename
        if not path.exists():
            path.write_text(heading + "\n\n", encoding="utf-8")
    return root


def space_statistics(session: Session, space: WikiSpace) -> dict[str, Any]:
    if space.id is None:
        return {
            "document_count": 0,
            "page_count": 0,
            "pending_review_count": 0,
            "last_updated_at": space.updated_at,
        }
    sid = int(space.id)
    document_count = int(
        session.exec(
            select(func.count(Document.id)).where(
                Document.space_id == sid,
                Document.status != "deleted",
            )
        ).one()
        or 0
    )
    page_count = int(
        session.exec(select(func.count(WikiPageRow.id)).where(WikiPageRow.space_id == sid)).one()
        or 0
    )
    pending_review_count = int(
        session.exec(
            select(func.count(WikiReviewItem.id)).where(
                WikiReviewItem.space_id == sid,
                WikiReviewItem.status == "pending",
            )
        ).one()
        or 0
    )
    latest_values = [space.updated_at]
    for model in (Document, WikiPageRow, WikiReviewItem):
        column = getattr(model, "updated_at")
        value = session.exec(
            select(func.max(column)).where(getattr(model, "space_id") == sid)
        ).one()
        if value:
            latest_values.append(value)
    return {
        "document_count": document_count,
        "page_count": page_count,
        "pending_review_count": pending_review_count,
        "last_updated_at": max(latest_values),
    }


def space_to_dict(session: Session, space: WikiSpace) -> dict[str, Any]:
    return {
        "id": int(space.id or 0),
        "name": space.name,
        "slug": space.slug,
        "description": space.description or "",
        "status": space.status,
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
    "ensure_space_dirs",
    "get_default_space",
    "normalize_space_slug",
    "resolve_space",
    "resolve_space_id",
    "space_scope_clause",
    "slug_from_name",
    "space_root",
    "space_statistics",
    "space_to_dict",
    "validate_space_status",
]
