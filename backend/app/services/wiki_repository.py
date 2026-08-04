"""Revisioned, path-safe persistence for Wiki 2.0 pages.

Task 3 keeps page identity in SQLite and page content in Markdown, but exposes
one controlled write boundary.  Callers provide a validated page object (or a
raw Markdown candidate); they never provide a filesystem path.  A candidate is
validated in an isolated staging directory, then the database and formal file
are applied with compensating rollback if either side fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import config
from app.models.entities import WikiPageRevision, WikiPageRow, WikiPageSource
from app.services.wiki_schema import (
    WikiFrontmatter,
    WikiPage,
    WikiSource,
    parse_wiki_page,
    serialize_wiki_page,
    validate_page_key,
)
from app.services.wiki_staging import (
    PAGE_TYPE_DIRECTORIES,
    WikiStaging,
    relative_page_path,
)


class WikiRepositoryError(RuntimeError):
    """Base class for repository and atomic-apply failures."""


class WikiPageNotFoundError(WikiRepositoryError):
    pass


class WikiPageAlreadyExistsError(WikiRepositoryError):
    pass


class WikiPageFileError(WikiRepositoryError):
    pass


class WikiPageCorruptError(WikiRepositoryError):
    pass


class WikiAtomicApplyError(WikiRepositoryError):
    pass


@dataclass(frozen=True)
class WikiPageRecord:
    """A database row and its validated, on-disk Markdown representation."""

    row: WikiPageRow
    page: WikiPage
    path: Path
    content: str

    @property
    def frontmatter(self) -> WikiFrontmatter:
        return self.page.frontmatter

    @property
    def body(self) -> str:
        return self.page.body

    @property
    def page_key(self) -> str:
        return self.page.page_key

    @property
    def title(self) -> str:
        return self.page.title

    @property
    def page_type(self) -> str:
        return self.page.type

    @property
    def revision(self) -> int:
        return self.row.revision

    @property
    def status(self) -> str:
        return self.row.status

    @property
    def id(self) -> int | None:
        return self.row.id

    @property
    def raw_content(self) -> str:
        return self.content


@dataclass
class _FileSnapshot:
    exists: bool
    content: bytes | None


@dataclass
class _LockEntry:
    lock: threading.RLock
    users: int = 0


_LOCK_REGISTRY_GUARD = threading.Lock()
_PAGE_LOCKS: dict[str, _LockEntry] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@contextmanager
def page_key_lock(page_key: str) -> Iterator[None]:
    """Serialize writes for one stable key within this Python process."""

    key = validate_page_key(page_key)
    with _LOCK_REGISTRY_GUARD:
        entry = _PAGE_LOCKS.get(key)
        if entry is None:
            entry = _LockEntry(lock=threading.RLock())
            _PAGE_LOCKS[key] = entry
        entry.users += 1

    entry.lock.acquire()
    try:
        yield
    finally:
        entry.lock.release()
        with _LOCK_REGISTRY_GUARD:
            entry.users -= 1
            if entry.users == 0 and _PAGE_LOCKS.get(key) is entry:
                del _PAGE_LOCKS[key]


def _wiki_root() -> Path:
    return Path(config.WIKI_DIR).resolve()


def _assert_formal_path(path: Path) -> Path:
    root = _wiki_root()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("formal Wiki path must remain inside config.WIKI_DIR") from exc
    return resolved


def page_path(page_type: str, page_key: str) -> Path:
    """Resolve a page identity to a safe absolute path below ``WIKI_DIR``."""

    relative = relative_page_path(page_type, page_key)
    return _assert_formal_path(_wiki_root() / relative)


# Names used by later tasks and tests.
resolve_page_path = page_path
path_for_page = page_path


def content_hash(content: str | bytes) -> str:
    """Return the SHA256 hash stored for the canonical Markdown bytes."""

    payload = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(payload).hexdigest()


def _coerce_page(
    page: WikiPage | WikiFrontmatter | Mapping[str, Any] | str,
    body: str | None = None,
) -> WikiPage:
    if isinstance(page, WikiPage):
        if body is not None:
            raise TypeError("body must be omitted when passing a WikiPage")
        return page.model_copy(deep=True)
    if isinstance(page, WikiFrontmatter):
        return WikiPage(frontmatter=page.model_copy(deep=True), body=body or "")
    if isinstance(page, str):
        if body is not None:
            raise TypeError("body must be omitted when passing raw Wiki Markdown")
        return parse_wiki_page(page)
    if isinstance(page, Mapping):
        # serialize_wiki_page performs the same frontmatter validation used for
        # files, then parse it so the repository always works with one model.
        return parse_wiki_page(serialize_wiki_page(page, body))
    raise TypeError("page must be a WikiPage, WikiFrontmatter, mapping, or Markdown string")


def _canonical_page(page: WikiPage, *, revision: int, status: str | None = None) -> WikiPage:
    """Set repository-owned revision/date and merge duplicate source rows."""

    sources_by_document: dict[int, WikiSource] = {}
    for source in page.frontmatter.sources:
        existing = sources_by_document.get(source.document_id)
        if existing is None:
            sources_by_document[source.document_id] = source.model_copy(deep=True)
            continue
        sources_by_document[source.document_id] = WikiSource(
            document_id=source.document_id,
            chunk_ids=list(dict.fromkeys(existing.chunk_ids + source.chunk_ids)),
            clauses=list(dict.fromkeys(existing.clauses + source.clauses)),
        )

    values = page.frontmatter.model_dump(mode="python")
    values["sources"] = list(sources_by_document.values())
    values["revision"] = revision
    values["updated_at"] = date.today()
    if status is not None:
        values["status"] = status
    metadata = WikiFrontmatter.model_validate(values)
    return WikiPage(frontmatter=metadata, body=page.body)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _snapshot_frontmatter(page: WikiPage) -> str:
    return _json(page.frontmatter.model_dump(mode="json", exclude_none=True))


def _source_rows(session: Session, page_id: int) -> list[WikiPageSource]:
    return list(
        session.exec(
            select(WikiPageSource).where(WikiPageSource.page_id == page_id)
        ).all()
    )


def _sync_sources(session: Session, row: WikiPageRow, page: WikiPage) -> None:
    if row.id is None:
        raise ValueError("Wiki page row must be flushed before source links are written")
    old_sources = _source_rows(session, row.id)
    for old_source in old_sources:
        session.delete(old_source)
    # The table has a unique (page_id, document_id) constraint.  Flush deletes
    # before adding replacements so SQLAlchemy cannot order an INSERT ahead of
    # the matching DELETE during one later unit-of-work flush.
    if old_sources:
        session.flush()
    for source in page.frontmatter.sources:
        session.add(
            WikiPageSource(
                page_id=row.id,
                document_id=source.document_id,
                chunk_ids_json=_json(source.chunk_ids),
                clauses_json=_json(source.clauses),
            )
        )


def _atomic_replace_bytes(target: Path, payload: bytes) -> None:
    """Write using a same-directory temporary file and ``os.replace``."""

    target = _assert_formal_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Re-check after mkdir in case a symlink was introduced between path
    # calculation and the write.
    _assert_formal_path(target.parent)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _snapshot_file(path: Path) -> _FileSnapshot:
    path = _assert_formal_path(path)
    if not path.exists():
        return _FileSnapshot(exists=False, content=None)
    if not path.is_file():
        raise WikiPageFileError(f"Wiki page path is not a file: {path}")
    return _FileSnapshot(exists=True, content=path.read_bytes())


def _restore_file(path: Path, snapshot: _FileSnapshot) -> None:
    """Restore a pre-apply file; only the repository-owned target is touched."""

    path = _assert_formal_path(path)
    if snapshot.exists:
        if snapshot.content is None:
            raise WikiAtomicApplyError("file snapshot is missing its original bytes")
        _atomic_replace_bytes(path, snapshot.content)
        return
    if path.exists():
        if not path.is_file():
            raise WikiAtomicApplyError(f"cannot remove non-file Wiki target: {path}")
        path.unlink()


def _record_from_page(
    row: WikiPageRow,
    page: WikiPage,
    path: Path,
    content: str,
) -> WikiPageRecord:
    return WikiPageRecord(row=row, page=page, path=path, content=content)


class WikiRepository:
    """Small page repository used by the later governed ingest workflow."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _find_row(self, page_key: str) -> WikiPageRow | None:
        key = validate_page_key(page_key)
        return self.session.exec(
            select(WikiPageRow).where(WikiPageRow.page_key == key)
        ).first()

    def _read_row(self, row: WikiPageRow) -> WikiPageRecord:
        if row.id is None or not row.page_key:
            raise WikiPageCorruptError("Wiki page row has no stable page_key")
        try:
            path = page_path(row.page_type, row.page_key)
        except ValueError as exc:
            raise WikiPageCorruptError(
                f"Wiki page row has an invalid type/key: {row.page_type}/{row.page_key}"
            ) from exc
        if not path.is_file():
            raise WikiPageFileError(f"Wiki page file is missing: {path}")
        try:
            content = path.read_text(encoding="utf-8")
            page = parse_wiki_page(content)
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            raise WikiPageCorruptError(f"Could not parse Wiki page {row.page_key}") from exc
        if page.page_key != row.page_key or page.type != row.page_type:
            raise WikiPageCorruptError(
                f"Wiki page identity does not match row for {row.page_key}"
            )
        if row.content_hash and content_hash(content) != row.content_hash:
            raise WikiPageCorruptError(f"Wiki page content hash mismatch: {row.page_key}")
        return _record_from_page(row, page, path, content)

    def read(self, page_key: str) -> WikiPageRecord:
        """Read and validate one page by stable key."""

        key = validate_page_key(page_key)
        row = self._find_row(key)
        if row is None:
            raise WikiPageNotFoundError(f"Wiki page not found: {key}")
        return self._read_row(row)

    get = read

    def list(
        self,
        *,
        page_type: str | None = None,
        status: str | None = None,
        domain: str | None = None,
        include_archived: bool = True,
    ) -> list[WikiPageRecord]:
        """List validated pages in stable key order."""

        statement = select(WikiPageRow)
        if page_type is not None:
            # Path validation also rejects old, unsupported page types here.
            if page_type not in PAGE_TYPE_DIRECTORIES:
                raise ValueError(f"unsupported Wiki page type: {page_type!r}")
            statement = statement.where(WikiPageRow.page_type == page_type)
        if status is not None:
            statement = statement.where(WikiPageRow.status == status)
        if domain is not None:
            statement = statement.where(WikiPageRow.domain == domain)
        if not include_archived:
            statement = statement.where(WikiPageRow.status != "archived")
        statement = statement.order_by(WikiPageRow.page_key)
        return [self._read_row(row) for row in self.session.exec(statement).all()]

    list_pages = list

    def list_rows(
        self,
        *,
        page_type: str | None = None,
        status: str | None = None,
        include_archived: bool = True,
    ) -> list[WikiPageRow]:
        """List metadata without opening files (useful for maintenance jobs)."""

        statement = select(WikiPageRow)
        if page_type is not None:
            if page_type not in PAGE_TYPE_DIRECTORIES:
                raise ValueError(f"unsupported Wiki page type: {page_type!r}")
            statement = statement.where(WikiPageRow.page_type == page_type)
        if status is not None:
            statement = statement.where(WikiPageRow.status == status)
        if not include_archived:
            statement = statement.where(WikiPageRow.status != "archived")
        return list(self.session.exec(statement.order_by(WikiPageRow.page_key)).all())

    def create(
        self,
        page: WikiPage | WikiFrontmatter | Mapping[str, Any] | str,
        body: str | None = None,
        *,
        job_id: int | None = None,
        reason: str = "created",
    ) -> WikiPageRecord:
        """Create a new page and its first immutable revision."""

        candidate = _coerce_page(page, body)
        return self._apply(
            candidate,
            operation="create",
            job_id=job_id,
            reason=reason,
        )

    def update(
        self,
        page_key: str,
        page: WikiPage | WikiFrontmatter | Mapping[str, Any] | str,
        body: str | None = None,
        *,
        job_id: int | None = None,
        reason: str = "updated",
    ) -> WikiPageRecord:
        """Replace a page's complete content while keeping its stable key."""

        key = validate_page_key(page_key)
        candidate = _coerce_page(page, body)
        if candidate.page_key != key:
            raise ValueError("update page_key does not match the candidate page")
        return self._apply(
            candidate,
            operation="update",
            job_id=job_id,
            reason=reason,
        )

    def archive(
        self,
        page_key: str,
        *,
        job_id: int | None = None,
        reason: str = "archived",
    ) -> WikiPageRecord:
        """Mark a page archived and retain its readable formal Markdown file."""

        key = validate_page_key(page_key)
        return self._apply(
            None,
            page_key=key,
            operation="archive",
            job_id=job_id,
            reason=reason,
        )

    def rollback(
        self,
        page_key: str,
        page: WikiPage | WikiFrontmatter | Mapping[str, Any] | str,
        body: str | None = None,
        *,
        job_id: int | None = None,
        reason: str = "rollback",
    ) -> WikiPageRecord:
        """Restore historical content as a new immutable rollback revision."""

        key = validate_page_key(page_key)
        candidate = _coerce_page(page, body)
        if candidate.page_key != key:
            raise ValueError("rollback page_key does not match the historical page")
        return self._apply(
            candidate,
            operation="rollback",
            job_id=job_id,
            reason=reason,
        )

    def _apply(
        self,
        candidate: WikiPage | None,
        *,
        operation: str,
        page_key: str | None = None,
        job_id: int | None,
        reason: str,
    ) -> WikiPageRecord:
        if operation not in {"create", "update", "rollback", "archive"}:
            raise ValueError(f"unsupported Wiki repository operation: {operation}")
        if candidate is not None:
            key = candidate.page_key
        elif page_key is not None:
            key = validate_page_key(page_key)
        else:
            raise ValueError("page_key is required when no candidate page is provided")

        with page_key_lock(key):
            existing = self._find_row(key)
            if operation == "create":
                if existing is not None:
                    raise WikiPageAlreadyExistsError(f"Wiki page already exists: {key}")
                if candidate is None:
                    raise ValueError("create requires a candidate page")
                target_revision = 1
                prepared = _canonical_page(candidate, revision=target_revision)
                old_row = None
            else:
                if existing is None:
                    raise WikiPageNotFoundError(f"Wiki page not found: {key}")
                old_row = existing
                old_record = self._read_row(existing)
                if operation == "archive":
                    if existing.status == "archived":
                        return old_record
                    prepared = _canonical_page(
                        old_record.page,
                        revision=max(1, existing.revision) + 1,
                        status="archived",
                    )
                else:
                    if candidate is None:
                        raise ValueError("update requires a candidate page")
                    if candidate.type != existing.page_type:
                        raise ValueError("page type cannot change during a page update")
                    prepared = _canonical_page(
                        candidate,
                        revision=max(1, existing.revision) + 1,
                    )
                target_revision = prepared.frontmatter.revision

            target = page_path(prepared.type, prepared.page_key)
            snapshot = _snapshot_file(target)
            if operation == "create" and snapshot.exists:
                raise WikiPageFileError(
                    f"formal Wiki file already exists for a new page: {target}"
                )
            if operation != "create" and not snapshot.exists:
                raise WikiPageFileError(f"formal Wiki file is missing: {target}")

            file_applied = False
            try:
                with WikiStaging() as staging:
                    staged = staging.stage_page(prepared)
                    # ``stage_page`` already reparses; this explicit second
                    # call keeps the apply boundary obvious and catches a
                    # candidate changed by a test or another process.
                    staged = staging.validate_staged_page(
                        staged.path,
                        page_type=prepared.type,
                        page_key=prepared.page_key,
                    )
                    formal_content = staged.content

                    if operation == "create":
                        row = WikiPageRow(
                            path=staged.relative_path,
                            title=prepared.title,
                            page_type=prepared.type,
                            source_document_id=(
                                prepared.frontmatter.sources[0].document_id
                                if prepared.frontmatter.sources
                                else None
                            ),
                            tags_json=_json(prepared.frontmatter.tags),
                            page_key=prepared.page_key,
                            domain=prepared.frontmatter.domain,
                            status=prepared.frontmatter.status,
                            revision=target_revision,
                            aliases_json=_json(prepared.frontmatter.aliases),
                            content_hash=content_hash(formal_content),
                        )
                        self.session.add(row)
                        self.session.flush()
                    else:
                        row = old_row
                        assert row is not None
                        row.path = staged.relative_path
                        row.title = prepared.title
                        row.page_type = prepared.type
                        row.source_document_id = (
                            prepared.frontmatter.sources[0].document_id
                            if prepared.frontmatter.sources
                            else None
                        )
                        row.tags_json = _json(prepared.frontmatter.tags)
                        row.page_key = prepared.page_key
                        row.domain = prepared.frontmatter.domain
                        row.status = prepared.frontmatter.status
                        row.revision = target_revision
                        row.aliases_json = _json(prepared.frontmatter.aliases)
                        row.content_hash = content_hash(formal_content)
                        row.updated_at = _utcnow()
                        self.session.add(row)
                        self.session.flush()

                    _sync_sources(self.session, row, prepared)
                    revision = WikiPageRevision(
                        page_id=row.id,
                        revision=target_revision,
                        frontmatter_json=_snapshot_frontmatter(prepared),
                        content_md=prepared.body,
                        operation=operation,
                        job_id=job_id,
                        reason=reason,
                    )
                    self.session.add(revision)
                    self.session.flush()

                    try:
                        from app.services.wiki_fts import upsert_wiki_page

                        with self.session.begin_nested():
                            upsert_wiki_page(
                                self.session,
                                row,
                                prepared.body,
                                frontmatter=prepared.frontmatter,
                            )
                    except Exception:
                        # The FTS table is a rebuildable projection. A missing
                        # SQLite extension must not make the canonical apply fail.
                        pass

                    _atomic_replace_bytes(target, formal_content.encode("utf-8"))
                    file_applied = True
                    self.session.commit()

                    return _record_from_page(row, prepared, target, formal_content)
            except IntegrityError as exc:
                self.session.rollback()
                if file_applied:
                    try:
                        _restore_file(target, snapshot)
                    except Exception as restore_exc:
                        raise WikiAtomicApplyError(
                            f"Wiki DB conflict and file restoration failed for {key}"
                        ) from restore_exc
                if operation == "create":
                    raise WikiPageAlreadyExistsError(
                        f"Wiki page already exists: {key}"
                    ) from exc
                raise
            except Exception:
                self.session.rollback()
                if file_applied:
                    try:
                        _restore_file(target, snapshot)
                    except Exception as restore_exc:
                        raise WikiAtomicApplyError(
                            f"Wiki file restoration failed after {operation}: {key}"
                        ) from restore_exc
                raise


# A shorter name is useful in Task 7 imports and keeps the module's public
# surface compatible with callers that use repository terminology.
PageRepository = WikiRepository


__all__ = [
    "PAGE_TYPE_DIRECTORIES",
    "PageRepository",
    "WikiAtomicApplyError",
    "WikiPageAlreadyExistsError",
    "WikiPageCorruptError",
    "WikiPageFileError",
    "WikiPageNotFoundError",
    "WikiPageRecord",
    "WikiRepository",
    "WikiRepositoryError",
    "content_hash",
    "page_key_lock",
    "page_path",
    "path_for_page",
    "relative_page_path",
    "resolve_page_path",
]
