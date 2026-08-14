from __future__ import annotations

import json
import difflib
import hashlib
import os
import shutil
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlmodel import Session, select

from app import config
from app.config import RETRIEVE_SOURCE_TOP_K, RETRIEVE_TOP_K, RETRIEVE_WIKI_TOP_K
from app.db import get_session
from app.models.entities import (
    IngestJob,
    SourceChunk,
    WikiPageRevision,
    WikiPageRow,
    WikiPageSource,
    WikiReviewItem,
    WikiSpace,
    User,
)
from app.schemas.documents import SourceChunkOut
from app.schemas.wiki import (
    IngestJobOut,
    RetrieveHit,
    RetrieveRequest,
    RetrieveResponse,
    WikiIndexOut,
    WikiPageOut,
    WikiCandidateOut,
    WikiDiffOut,
    WikiReviewDecisionIn,
    WikiReviewBatchIn,
    WikiReviewBatchOut,
    WikiReviewBatchSkipOut,
    WikiReviewDetailOut,
    WikiReviewOut,
    WikiReviewReasonOut,
    WikiRevisionOut,
    WikiRollbackIn,
    WikiRollbackOut,
    WikiSourceEvidenceOut,
    WikiPurgePreviewOut,
    WikiPurgeExecuteIn,
    WikiPurgeExecuteOut,
    WikiPurgeSpaceOut,
    WikiPurgeCountsOut,
)
from app.services.retrieve import load_all_wiki_pages, rank_pages
from app.services.source_chunks_store import load_all_source_chunks, rank_source_chunks
from app.services.wiki_jobs import cancel_ingest_job, retry_failed_windows
from app.services.wiki_index import rebuild_index
from app.services.wiki_fts import rebuild_fts
from app.services.wiki_log import log_review, log_rollback
from app.services.wiki_overview import rebuild_overview
from app.services.wiki_repository import (
    WikiPageAlreadyExistsError,
    WikiPageNotFoundError,
    WikiRepository,
    page_key_lock,
    page_path,
)
from app.services.wiki_schema import WikiFrontmatter, WikiPage, WikiSource
from app.services.wiki_titles import display_title, is_technical_title
from app.services.wiki_spaces import (
    resolve_space,
    resolve_space_id,
    space_root,
    space_scope_clause,
)

router = APIRouter(tags=["wiki"])

_PURGE_LOCK = threading.Lock()
_PURGE_CONFIRMATION_PREFIX = "PURGE_ARCHIVED_WIKI"
_OS_OPEN = os.open
_OS_UNLINK = os.unlink
_OS_RENAME = os.rename


def _require_admin(request: Request) -> None:
    # The test suite disables auth for legacy endpoints.  In production, the
    # middleware always supplies request.state.user before this boundary.
    if not config.AUTH_ENABLED:
        raise HTTPException(status_code=503, detail="Administrator purge is unavailable while authentication is disabled")
    user = getattr(request.state, "user", None)
    if user is None or not bool(getattr(user, "is_active", False)) or getattr(user, "role", "") != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required")


def _lexical_path(value: Path) -> Path:
    """Normalize a path without resolving symlinks."""
    return Path(os.path.abspath(os.fspath(value)))


def _relative_identifier(path: Path, *, row_id: int | None = None) -> str:
    root = _lexical_path(Path(config.WIKI_DIR))
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return f"page:{row_id or 'unknown'}"


def _canonical_purge_path(space: WikiSpace, row: WikiPageRow) -> Path | None:
    if not row.page_key:
        return None
    try:
        relative = page_path(row.page_type, row.page_key, space_slug=space.slug).relative_to(
            _lexical_path(Path(config.WIKI_DIR))
        )
    except (TypeError, ValueError):
        return None
    return _lexical_path(Path(config.WIKI_DIR) / relative)


def _legacy_owned_path(space: WikiSpace, candidate: Path) -> bool:
    """Whether a non-canonical legacy path is provably owned by a space."""
    root = _lexical_path(Path(config.WIKI_DIR))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return False
    # A row may only own a Markdown file below its space's pages subtree.  In
    # particular, never treat the space root (index/overview/log) or sibling
    # generated files as page-owned legacy content.
    space_prefix = Path("spaces") / str(space.slug) / "pages"
    if (
        len(relative.parts) >= 2
        and relative.parts[:3] == space_prefix.parts
        and relative.name not in {"index.md", "overview.md", "log.md"}
    ):
        return True
    # Pre-space rows used wiki/pages/<name>.md and are compatible only with
    # the default space.  The old type directories are deliberately not
    # accepted: they can contain indexes and other space-level artifacts.
    return (
        str(space.slug) == "default"
        and len(relative.parts) >= 2
        and relative.parts[0] == "pages"
        and relative.name not in {"index.md", "overview.md", "log.md"}
    )


def _row_purge_path(space: WikiSpace, row: WikiPageRow) -> tuple[Path, str | None, str | None]:
    """Return an owned, lexical path and a display identifier.

    ``row.path`` is authoritative for legacy rows.  For keyed rows a path
    mismatch is unsafe rather than silently deleting the generated canonical
    file.  No ``resolve()`` or ``exists()`` call is used before lstat: either
    operation can follow an attacker-controlled symlink.
    """
    raw = Path(str(row.path or ""))
    candidate = _lexical_path(raw if raw.is_absolute() else Path(config.WIKI_DIR) / raw)
    canonical = _canonical_purge_path(space, row)
    if canonical is not None and candidate != canonical and not _legacy_owned_path(space, candidate):
        return candidate, "row.path does not match canonical page path", _relative_identifier(candidate, row_id=row.id)
    root = _lexical_path(Path(config.WIKI_DIR))
    try:
        candidate.relative_to(root)
    except ValueError:
        return candidate, "path escapes configured Wiki root", _relative_identifier(candidate, row_id=row.id)

    # The authoritative check happens through the dir_fd/openat abstraction
    # below.  Keep this lexical check only for a useful preview diagnostic;
    # it never supplies the descriptor used by execute.
    parent_fd = None
    try:
        parent_fd = _open_secure_parent(candidate)
    except FileNotFoundError:
        pass
    except _PurgeUnsafeError as exc:
        return candidate, str(exc), _relative_identifier(candidate, row_id=row.id)
    except OSError as exc:
        return candidate, f"cannot inspect path: {exc}", _relative_identifier(candidate, row_id=row.id)
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
    return candidate, None, _relative_identifier(candidate, row_id=row.id)


def _safe_purge_path(path: Path) -> tuple[Path, str | None]:
    """Compatibility wrapper used by callers outside the snapshot builder."""
    candidate = _lexical_path(path)
    root = _lexical_path(Path(config.WIKI_DIR))
    try:
        candidate.relative_to(root)
    except ValueError:
        return candidate, "path escapes configured Wiki root"
    parent_fd = None
    try:
        parent_fd = _open_secure_parent(candidate)
    except FileNotFoundError:
        return candidate, None
    except _PurgeUnsafeError as exc:
        return candidate, str(exc)
    except OSError as exc:
        return candidate, f"cannot inspect path: {exc}"
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
    return candidate, None


class _PurgeUnsafeError(OSError):
    """A Wiki path failed the no-follow, directory-fd safety boundary."""


class _PurgeStaleError(OSError):
    """A file changed after the preview/backup fingerprint."""


def _purge_dirfd_supported() -> bool:
    # Keep capability detection stable if a caller monkeypatches an operation
    # for failure injection; the actual operation below still uses os.unlink.
    required = (_OS_OPEN, _OS_UNLINK, _OS_RENAME)
    return (
        all(operation in os.supports_dir_fd for operation in required)
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
    )


def _secure_relative_parts(path: Path) -> tuple[Path, tuple[str, ...]]:
    root = _lexical_path(Path(config.WIKI_DIR))
    candidate = _lexical_path(path)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise _PurgeUnsafeError("path escapes configured Wiki root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise _PurgeUnsafeError("invalid Wiki path")
    return root, relative.parts


def _open_secure_parent(path: Path) -> int:
    """Open the candidate's parent by walking trusted dirfds, never paths."""
    if not _purge_dirfd_supported():
        raise _PurgeUnsafeError("secure Wiki purge is unsupported on this platform")
    root, parts = _secure_relative_parts(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(root, flags)
    except OSError as exc:
        if exc.errno in {getattr(os, "ELOOP", 40), getattr(os, "ENOTDIR", 20)}:
            raise _PurgeUnsafeError("Wiki path contains a symlink or non-directory") from exc
        raise
    try:
        for component in parts[:-1]:
            try:
                next_fd = os.open(component, flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in {getattr(os, "ELOOP", 40), getattr(os, "ENOTDIR", 20)}:
                    raise _PurgeUnsafeError("Wiki path contains a symlink or non-directory") from exc
                raise
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _fingerprint_fd(fd: int) -> dict[str, Any]:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        return {"state": "unsafe", "mode": int(info.st_mode)}
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return {
        "state": "file",
        "dev": int(info.st_dev),
        "ino": int(info.st_ino),
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "digest": digest.hexdigest(),
    }


def _open_secure_file(path: Path) -> tuple[int, int, str]:
    parent_fd = _open_secure_parent(path)
    _root, parts = _secure_relative_parts(path)
    name = parts[-1]
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        os.close(parent_fd)
        if exc.errno in {getattr(os, "ELOOP", 40), getattr(os, "ENOTDIR", 20)}:
            raise _PurgeUnsafeError("Wiki path contains a symlink or non-regular file") from exc
        raise
    return fd, parent_fd, name


def _path_fingerprint(path: Path) -> dict[str, Any]:
    try:
        fd, parent_fd, _name = _open_secure_file(path)
    except FileNotFoundError:
        return {"state": "missing"}
    except _PurgeUnsafeError as exc:
        return {"state": "unsafe", "error": str(exc)}
    try:
        return _fingerprint_fd(fd)
    finally:
        os.close(fd)
        os.close(parent_fd)


def _backup_file(path: Path, backup_root: Path) -> Path | None:
    """Copy one regular file through a fixed parent directory descriptor."""
    fd = parent_fd = None
    try:
        try:
            fd, parent_fd, _name = _open_secure_file(path)
        except FileNotFoundError:
            return None
        rel = _secure_relative_parts(path)[1]
        target = backup_root / Path(*rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(fd, "rb", closefd=False) as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)
        return target
    except FileNotFoundError:
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _restore_backups(backups: list[tuple[Path, Path]]) -> list[str]:
    failures: list[str] = []
    for original, backup in backups:
        parent_fd = None
        try:
            parent_fd = _open_secure_parent(original)
            _root, parts = _secure_relative_parts(original)
            os.rename(os.fspath(backup), parts[-1], dst_dir_fd=parent_fd)
        except FileNotFoundError:
            failures.append(f"{original}: backup or destination parent is missing")
        except _PurgeUnsafeError as exc:
            failures.append(f"{original}: {exc}")
        except Exception as exc:
            failures.append(f"{original}: {exc}")
        finally:
            if parent_fd is not None:
                try:
                    os.close(parent_fd)
                except OSError:
                    pass
    return failures


def _unlink_secure_file(path: Path, expected: dict[str, Any]) -> None:
    """Verify and unlink using one fixed parent descriptor."""
    fd = parent_fd = None
    try:
        fd, parent_fd, name = _open_secure_file(path)
        actual = _fingerprint_fd(fd)
        if actual != expected:
            raise _PurgeStaleError("path changed after backup")
        os.unlink(name, dir_fd=parent_fd)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _stable_datetime(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def _child_plan_records(
    revisions: list[WikiPageRevision],
    sources: list[WikiPageSource],
    reviews: list[WikiReviewItem],
    page_id: int,
) -> dict[str, list[dict[str, Any]]]:
    """Return deterministic child content, not only database identifiers."""
    return {
        "revisions": [
            {
                "id": int(item.id or 0), "page_id": int(item.page_id), "revision": int(item.revision),
                "frontmatter_json": item.frontmatter_json or "", "content_md": item.content_md or "",
                "operation": item.operation or "", "job_id": int(item.job_id) if item.job_id is not None else None,
                "reason": item.reason or "", "created_at": _stable_datetime(item.created_at),
            }
            for item in sorted((item for item in revisions if item.page_id == page_id), key=lambda value: int(value.id or 0))
        ],
        "page_sources": [
            {
                "id": int(item.id or 0), "page_id": int(item.page_id), "document_id": int(item.document_id),
                "chunk_ids_json": item.chunk_ids_json or "[]", "clauses_json": item.clauses_json or "[]",
                "created_at": _stable_datetime(item.created_at), "updated_at": _stable_datetime(item.updated_at),
            }
            for item in sorted((item for item in sources if item.page_id == page_id), key=lambda value: int(value.id or 0))
        ],
        "reviews": [
            {
                "id": int(item.id or 0), "page_id": int(item.page_id) if item.page_id is not None else None,
                "job_id": int(item.job_id) if item.job_id is not None else None,
                "space_id": int(item.space_id) if item.space_id is not None else None,
                "kind": item.kind or "", "status": item.status or "", "reason": item.reason or "",
                "candidate_frontmatter_json": item.candidate_frontmatter_json,
                "candidate_content_md": item.candidate_content_md,
                "payload_json": item.payload_json or "{}", "reviewed_at": _stable_datetime(item.reviewed_at),
                "reviewed_by": item.reviewed_by, "decision_reason": item.decision_reason,
                "created_at": _stable_datetime(item.created_at), "updated_at": _stable_datetime(item.updated_at),
            }
            for item in sorted((item for item in reviews if item.page_id == page_id), key=lambda value: int(value.id or 0))
        ],
    }


def _purge_snapshot(session: Session) -> dict[str, Any]:
    spaces = session.exec(select(WikiSpace).order_by(WikiSpace.id)).all()
    grouped: list[dict[str, Any]] = []
    missing: list[str] = []
    unsafe: list[str] = []
    active_jobs: list[int] = []
    all_plan: list[dict[str, Any]] = []
    for space in spaces:
        if space.id is None:
            continue
        rows = session.exec(select(WikiPageRow).where(
            WikiPageRow.status == "archived",
            space_scope_clause(session, WikiPageRow.space_id, int(space.id)),
        ).order_by(WikiPageRow.id)).all()
        page_ids = [int(row.id) for row in rows if row.id is not None]
        revisions = session.exec(select(WikiPageRevision).where(WikiPageRevision.page_id.in_(page_ids))).all() if page_ids else []
        sources = session.exec(select(WikiPageSource).where(WikiPageSource.page_id.in_(page_ids))).all() if page_ids else []
        reviews = session.exec(select(WikiReviewItem).where(WikiReviewItem.page_id.in_(page_ids))).all() if page_ids else []
        jobs = session.exec(select(IngestJob).where(
            space_scope_clause(session, IngestJob.space_id, int(space.id)),
            IngestJob.status.in_(["queued", "running"]),
        )).all()
        active_jobs.extend(int(job.id) for job in jobs if job.id is not None)
        files = 0
        for row in rows:
            path, problem, identifier = _row_purge_path(space, row)
            fingerprint = _path_fingerprint(path)
            if problem:
                unsafe.append(f"{row.id}:{problem}")
            elif fingerprint["state"] == "file":
                files += 1
            elif fingerprint["state"] == "missing":
                missing.append(identifier)
            else:
                unsafe.append(f"{row.id}:path is not a regular file")
            child_ids = _child_plan_records(revisions, sources, reviews, int(row.id))
            all_plan.append({
                "space_id": int(space.id),
                "space_slug": space.slug,
                "page_id": int(row.id or 0),
                "revision": int(row.revision or 0),
                "content_hash": row.content_hash or "",
                "path": str(row.path or ""),
                "path_identifier": identifier,
                "file": fingerprint,
                "children": child_ids,
                "status": row.status,
            })
        grouped.append({"space_id": int(space.id), "space_name": space.name, "space_slug": space.slug, "pages": len(rows), "revisions": len(revisions), "page_sources": len(sources), "reviews": len(reviews), "files": files})
    all_plan.sort(key=lambda item: (item["space_id"], item["page_id"]))
    digest = hashlib.sha256(json.dumps(all_plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    totals = {key: sum(int(item[key]) for item in grouped) for key in ("pages", "revisions", "page_sources", "reviews", "files")}
    return {"spaces": grouped, "totals": totals, "missing": missing, "unsafe": unsafe, "active_jobs": sorted(set(active_jobs)), "plan_hash": digest, "confirmation_text": f"{_PURGE_CONFIRMATION_PREFIX}:{digest}", "plan": all_plan}


@router.get("/api/admin/wiki/purge/preview", response_model=WikiPurgePreviewOut)
def preview_archived_wiki_purge(
    request: Request,
    session: Session = Depends(get_session),
) -> WikiPurgePreviewOut:
    _require_admin(request)
    snapshot = _purge_snapshot(session)
    warnings: list[str] = []
    if snapshot["missing"]:
        warnings.append("部分归档 Markdown 已缺失，将仅清理数据库记录")
    if snapshot["unsafe"]:
        warnings.append("存在不安全路径，执行将被阻止")
    if snapshot["active_jobs"]:
        warnings.append("存在 queued/running 摄入任务，执行将被阻止")
    return WikiPurgePreviewOut(
        scope="all",
        confirmation_text=snapshot["confirmation_text"],
        plan_hash=snapshot["plan_hash"],
        spaces=[WikiPurgeSpaceOut.model_validate(item) for item in snapshot["spaces"]],
        totals=WikiPurgeCountsOut.model_validate(snapshot["totals"]),
        missing=snapshot["missing"],
        unsafe=snapshot["unsafe"],
        active_jobs=snapshot["active_jobs"],
        warnings=warnings,
    )


@router.post("/api/admin/wiki/purge", response_model=WikiPurgeExecuteOut)
def execute_archived_wiki_purge(
    body: WikiPurgeExecuteIn,
    request: Request,
    session: Session = Depends(get_session),
) -> WikiPurgeExecuteOut:
    _require_admin(request)
    if body.scope != "all":
        raise HTTPException(status_code=422, detail="Only all scope is supported")
    if not _PURGE_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another purge is already running")
    backups: list[tuple[Path, Path]] = []
    expected_files: dict[Path, dict[str, Any]] = {}
    backup_root: Path | None = None
    try:
        current = _purge_snapshot(session)
        if body.plan_hash != current["plan_hash"] or body.confirmation_text != current["confirmation_text"]:
            raise HTTPException(status_code=409, detail="Purge plan has changed; preview again")
        if current["unsafe"]:
            raise HTTPException(status_code=422, detail="Unsafe Wiki path blocks purge")
        if current["active_jobs"]:
            raise HTTPException(status_code=409, detail="Active ingest jobs block purge")
        # Serialize writers at the database level as well as in-process.  The
        # snapshot and the destructive second check now run under one SQLite
        # write transaction, reducing cross-process races (the process lock is
        # still retained for the common single-worker case).
        session.rollback()
        session.exec(text("BEGIN IMMEDIATE"))
        locked = _purge_snapshot(session)
        if body.plan_hash != locked["plan_hash"] or body.confirmation_text != locked["confirmation_text"]:
            raise HTTPException(status_code=409, detail="Purge plan has changed; preview again")
        if locked["unsafe"]:
            raise HTTPException(status_code=422, detail="Unsafe Wiki path blocks purge")
        if locked["active_jobs"]:
            raise HTTPException(status_code=409, detail="Active ingest jobs block purge")
        current = locked
        plan = current["plan"]
        if not plan:
            session.rollback()
            return WikiPurgeExecuteOut(status="completed_already_purged", plan_hash=current["plan_hash"], counts=WikiPurgeCountsOut())
        page_ids = [int(item["page_id"]) for item in plan]
        rows = session.exec(select(WikiPageRow).where(WikiPageRow.id.in_(page_ids))).all()
        row_map = {int(row.id): row for row in rows if row.id is not None}
        for item in plan:
            row = row_map.get(int(item["page_id"]))
            if row is None or row.status != "archived" or int(row.revision or 0) != int(item["revision"]):
                raise HTTPException(status_code=409, detail="Purge plan is stale; preview again")
            space = session.get(WikiSpace, int(item["space_id"]))
            if space is None:
                raise HTTPException(status_code=409, detail="Purge plan is stale; preview again")
            path, problem, _identifier = _row_purge_path(space, row)
            if problem or _path_fingerprint(path) != item["file"]:
                raise HTTPException(status_code=409, detail="Purge plan is stale; preview again")
            children = _child_plan_records(
                session.exec(select(WikiPageRevision).where(WikiPageRevision.page_id == row.id)).all(),
                session.exec(select(WikiPageSource).where(WikiPageSource.page_id == row.id)).all(),
                session.exec(select(WikiReviewItem).where(WikiReviewItem.page_id == row.id)).all(),
                int(row.id),
            )
            if children != item["children"]:
                raise HTTPException(status_code=409, detail="Purge plan is stale; preview again")
            if item["file"].get("state") not in {"file", "missing"}:
                raise HTTPException(status_code=422, detail="Unsafe Wiki path blocks purge")
        backup_root = Path(tempfile.mkdtemp(prefix="casegen-wiki-purge-", dir=str(config.DATA_DIR)))
        for item in plan:
            row = row_map[int(item["page_id"])]
            space = session.get(WikiSpace, int(item["space_id"]))
            path, problem, _identifier = _row_purge_path(space, row)
            if problem:
                raise HTTPException(status_code=422, detail="Unsafe Wiki path blocks purge")
            try:
                backup = _backup_file(path, backup_root) if item["file"].get("state") == "file" else None
            except _PurgeUnsafeError as exc:
                raise HTTPException(status_code=422, detail="Unsafe Wiki path blocks purge") from exc
            except _PurgeStaleError as exc:
                raise HTTPException(status_code=409, detail="Purge plan is stale; preview again") from exc
            if item["file"].get("state") == "file" and backup is None:
                raise HTTPException(status_code=409, detail="Purge plan is stale; preview again")
            if backup is not None:
                backups.append((path, backup))
                expected_files[path] = item["file"]
        for page_id in page_ids:
            for model in (WikiPageSource, WikiPageRevision, WikiReviewItem):
                for child in session.exec(select(model).where(model.page_id == page_id)).all():
                    session.delete(child)
            row = session.get(WikiPageRow, page_id)
            if row is not None:
                session.delete(row)
        session.flush()
        try:
            for path, _backup in backups:
                _unlink_secure_file(path, expected_files[path])
        except Exception as exc:
            session.rollback()
            restore_failures = _restore_backups(backups)
            detail = f"Purge failed; files restored: {exc}"
            if restore_failures:
                detail += "; CRITICAL restore failure: " + ", ".join(restore_failures)
            if isinstance(exc, _PurgeUnsafeError):
                status_code = 422
            elif isinstance(exc, _PurgeStaleError):
                status_code = 409
            else:
                status_code = 500
            raise HTTPException(status_code=status_code, detail=detail) from exc
        try:
            session.commit()
        except Exception as exc:
            session.rollback()
            restore_failures = _restore_backups(backups)
            detail = f"Purge database commit failed; files restored: {exc}"
            if restore_failures:
                detail += "; CRITICAL restore failure: " + ", ".join(restore_failures)
            raise HTTPException(status_code=500, detail=detail) from exc
        if backup_root is not None:
            shutil.rmtree(backup_root, ignore_errors=True)
            backup_root = None
        warnings: list[str] = []
        space_ids = sorted({int(item["space_id"]) for item in plan})
        for space_id in space_ids:
            try:
                remaining = session.exec(select(WikiPageRow).where(space_scope_clause(session, WikiPageRow.space_id, space_id))).all()
                rebuild_index(remaining, session=session, space_id=space_id)
                rebuild_overview(remaining, session=session, space_id=space_id)
                rebuild_fts(session, space_id=space_id)
                session.commit()
            except Exception as exc:
                session.rollback()
                warnings.append(f"space {space_id} derived rebuild failed: {exc}")
        return WikiPurgeExecuteOut(status="completed_with_warnings" if warnings else "completed", plan_hash=current["plan_hash"], counts=WikiPurgeCountsOut.model_validate(current["totals"]), warnings=warnings)
    finally:
        if backup_root is not None:
            shutil.rmtree(backup_root, ignore_errors=True)
        _PURGE_LOCK.release()


def _space_for_row(session: Session, row: WikiPageRow):
    try:
        space = resolve_space(session, row.space_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row.space_id is None:
        row.space_id = space.id
        session.add(row)
    return space


def _job_space(session: Session, job: IngestJob):
    try:
        space = resolve_space(session, job.space_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job.space_id is None:
        job.space_id = space.id
        session.add(job)
    return space


def _job_out(session: Session, job: IngestJob) -> IngestJobOut:
    space = _job_space(session, job)
    return IngestJobOut.model_validate(
        {
            **job.model_dump(),
            "space_id": int(space.id or 0),
            "space_name": space.name,
        }
    )


def _job_scope(session: Session, job: IngestJob, space_id: int) -> None:
    """Treat legacy NULL jobs as default-space data only."""

    default_space_id = resolve_space_id(session)
    actual_space_id = int(job.space_id) if job.space_id is not None else default_space_id
    if actual_space_id != int(space_id):
        raise HTTPException(status_code=404, detail="Ingest job not found in this space")


def _page_scope(session: Session, row: WikiPageRow, space_id: int | None) -> Any:
    space = _space_for_row(session, row)
    if space_id is not None and int(space.id or 0) != int(space_id):
        raise HTTPException(status_code=404, detail="Wiki page not found in this space")
    return space


def _review_scope(session: Session, item: WikiReviewItem, space_id: int) -> None:
    """Treat legacy NULL reviews as default-space data only."""

    default_space_id = resolve_space_id(session)
    actual_space_id = int(item.space_id) if item.space_id is not None else default_space_id
    if actual_space_id != int(space_id):
        raise HTTPException(status_code=404, detail="Wiki review item not found in this space")


def _tags_from_row(row: WikiPageRow) -> list[str]:
    try:
        tags = json.loads(row.tags_json or "[]")
    except json.JSONDecodeError:
        tags = []
    if not isinstance(tags, list):
        return []
    return [str(t) for t in tags]


def _aliases_from_row(row: WikiPageRow) -> list[str]:
    try:
        aliases = json.loads(row.aliases_json or "[]")
    except json.JSONDecodeError:
        aliases = []
    if not isinstance(aliases, list):
        return []
    return [str(alias) for alias in aliases]


def _read_page_content(row: WikiPageRow) -> str:
    path = Path(row.path or "")
    if not path.is_absolute():
        candidate = config.WIKI_DIR / (row.path or "")
        if not candidate.exists():
            candidate = config.WIKI_PAGES_DIR / Path(row.path or "").name
        path = candidate
    try:
        path.resolve().relative_to(config.WIKI_DIR.resolve())
    except ValueError:
        return ""
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")
    return ""


def _to_page_out(
    row: WikiPageRow,
    *,
    include_content: bool = False,
    session: Session | None = None,
) -> WikiPageOut:
    space = None
    if session is not None:
        space = _space_for_row(session, row)
    needs_title_fallback = is_technical_title(row.title, row.page_key)
    content = _read_page_content(row) if include_content or needs_title_fallback else None
    title = display_title(
        row.title,
        page_key=row.page_key,
        page_type=row.page_type,
        body=content or "",
    )
    return WikiPageOut(
        id=row.id,
        path=row.path,
        space_id=int(space.id if space and space.id is not None else row.space_id or 0),
        space_name=space.name if space else "",
        title=title,
        page_type=row.page_type,
        source_document_id=row.source_document_id,
        page_key=row.page_key,
        domain=row.domain,
        status=row.status,
        revision=row.revision,
        aliases=_aliases_from_row(row),
        tags=_tags_from_row(row),
        content=content if include_content else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _source_evidence(value: Any) -> list[WikiSourceEvidenceOut]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    result: list[WikiSourceEvidenceOut] = []
    for item in value:
        try:
            source = WikiSource.model_validate(item)
        except (TypeError, ValueError):
            continue
        result.append(
            WikiSourceEvidenceOut(
                document_id=source.document_id,
                chunk_ids=list(source.chunk_ids),
                clauses=list(source.clauses),
            )
        )
    return result


def _revision_out(revision: WikiPageRevision) -> WikiRevisionOut:
    return WikiRevisionOut(
        id=int(revision.id or 0),
        page_id=revision.page_id,
        revision=revision.revision,
        frontmatter=_json_object(revision.frontmatter_json),
        frontmatter_json=revision.frontmatter_json or "{}",
        content_md=revision.content_md or "",
        operation=revision.operation,
        job_id=revision.job_id,
        reason=revision.reason,
        created_at=revision.created_at,
    )


def _revision_for_page(
    session: Session, page_id: int, revision_number: int | None = None
) -> WikiPageRevision | None:
    statement = select(WikiPageRevision).where(WikiPageRevision.page_id == page_id)
    if revision_number is not None:
        statement = statement.where(WikiPageRevision.revision == revision_number)
    return session.exec(statement.order_by(WikiPageRevision.revision.desc())).first()


def _candidate_meta(item: WikiReviewItem) -> dict[str, Any]:
    return _json_object(item.candidate_frontmatter_json)


def _review_summary(item: WikiReviewItem, session: Session | None = None) -> WikiReviewOut:
    space = None
    if session is not None:
        try:
            space = resolve_space(session, item.space_id)
        except ValueError:
            space = None
    return WikiReviewOut(
        id=int(item.id or 0),
        page_id=item.page_id,
        job_id=item.job_id,
        space_id=item.space_id or (space.id if space else None),
        space_name=space.name if space else "",
        kind=item.kind,
        status=item.status,
        reason=item.reason,
        candidate_available=bool(item.candidate_content_md and _candidate_meta(item)),
        reviewed_at=item.reviewed_at,
        reviewed_by=item.reviewed_by,
        decision_reason=item.decision_reason,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _review_detail(session: Session, item: WikiReviewItem) -> WikiReviewDetailOut:
    meta = _candidate_meta(item)
    payload = _json_object(item.payload_json)
    page_key = str(meta.get("page_key") or payload.get("page_key") or "") or None
    page_type = meta.get("type") or meta.get("page_type")
    sources = _source_evidence(meta.get("sources"))
    if not sources and item.job_id is not None:
        job = session.get(IngestJob, item.job_id)
        if job is not None:
            _job_space(session, job)
            sources = [WikiSourceEvidenceOut(document_id=job.document_id)]
    candidate = WikiCandidateOut(
        page_key=page_key,
        title=str(meta.get("title")) if meta.get("title") is not None else None,
        type=str(page_type) if page_type else None,
        domain=str(meta.get("domain")) if meta.get("domain") is not None else None,
        aliases=_string_list(meta.get("aliases")),
        tags=_string_list(meta.get("tags")),
        status=str(meta.get("status")) if meta.get("status") is not None else None,
        sources=sources,
        content_md=item.candidate_content_md,
        raw={**meta, "content_md": item.candidate_content_md},
    )

    old_revision = None
    if item.page_id is not None:
        revisions = session.exec(
            select(WikiPageRevision)
            .where(WikiPageRevision.page_id == item.page_id)
            .order_by(WikiPageRevision.revision.desc())
        ).all()
        operation = str(payload.get("operation") or "")
        if item.status == "approved" and operation == "create":
            old_revision = None
        elif item.status == "approved" and operation == "update" and len(revisions) > 1:
            old_revision = revisions[1]
        elif revisions:
            old_revision = revisions[0]
    old_out = _revision_out(old_revision) if old_revision is not None else None
    old_content = old_out.content_md if old_out is not None else ""
    new_content = item.candidate_content_md or ""
    operation = str(payload.get("operation") or "")
    is_writable_candidate = (
        operation in {"create", "update"}
        and bool(new_content.strip())
        and bool(meta)
    )
    if is_writable_candidate:
        unified = "".join(
            difflib.unified_diff(
                old_content.splitlines(True),
                new_content.splitlines(True),
                fromfile="old Wiki revision",
                tofile="candidate Wiki revision",
            )
        )
        diff = WikiDiffOut(
            from_revision=old_out.revision if old_out else None,
            to_revision=None,
            unified=unified,
            text=unified,
            changed=old_content != new_content,
            available=True,
        )
    else:
        diff = WikiDiffOut(
            available=False,
            reason=(
                "这是结构化审核提醒，不包含待写入的页面候选内容；"
                "请结合来源证据确认后标记为已处理。"
            ),
        )
    risks = payload.get("risk_flags")
    if isinstance(risks, str):
        risks = [risks]
    if not isinstance(risks, list):
        risks = []
    reason_detail = WikiReviewReasonOut(
        summary=item.reason,
        kind=item.kind,
        operation=operation or None,
        page_key=page_key,
        risk_flags=[str(value) for value in risks],
    )
    return WikiReviewDetailOut(
        **_review_summary(item, session).model_dump(),
        old_version=old_out,
        new_candidate=candidate,
        reason_detail=reason_detail,
        payload=payload,
        source_evidence=sources,
        diff=diff,
    )


def _candidate_page(
    session: Session, item: WikiReviewItem, operation: str
) -> tuple[str, WikiPage]:
    meta = _candidate_meta(item)
    payload = _json_object(item.payload_json)
    if not item.candidate_frontmatter_json or not meta:
        raise ValueError("candidate metadata is missing or invalid")
    row = session.get(WikiPageRow, item.page_id) if item.page_id is not None else None
    if operation == "update":
        if row is None or not row.page_key:
            raise ValueError("update candidate requires an existing page")
        space_id = row.space_id
        if item.space_id is not None:
            space_id = item.space_id
        elif item.job_id is not None:
            job_row = session.get(IngestJob, item.job_id)
            if job_row is not None:
                space_id = job_row.space_id
        existing = WikiRepository(session, space_id=space_id).read(row.page_key)
        values = existing.frontmatter.model_dump(mode="python")
        page_key = row.page_key
    else:
        existing = None
        page_key = str(meta.get("page_key") or payload.get("page_key") or "")
        values = {}
    if not page_key:
        raise ValueError("candidate page_key is missing")

    # Candidate metadata is untrusted.  Only frontmatter fields are copied;
    # operation, path-like values and arbitrary model fields never reach the
    # repository boundary.
    if meta.get("page_key") is not None:
        values["page_key"] = meta["page_key"]
    else:
        values["page_key"] = page_key
    for field in ("title", "domain", "status"):
        if field in meta and meta[field] is not None:
            values[field] = meta[field]
    for field in ("aliases", "tags"):
        if field in meta and meta[field] is not None:
            incoming = _string_list(meta[field])
            previous = _string_list(values.get(field)) if existing is not None else []
            values[field] = list(dict.fromkeys([*previous, *incoming]))
    if meta.get("type") is not None:
        values["type"] = meta["type"]
    elif meta.get("page_type") is not None:
        values["type"] = meta["page_type"]
    elif existing is None:
        values["type"] = "source" if page_key.startswith("source.") else "rule"

    raw_sources = meta.get("sources")
    if raw_sources is None:
        raw_sources = []
    if isinstance(raw_sources, dict):
        raw_sources = [raw_sources]
    if not isinstance(raw_sources, list):
        raise ValueError("candidate sources must be a list")
    try:
        parsed_sources = [WikiSource.model_validate(source) for source in raw_sources]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid candidate source evidence: {exc}") from exc
    source_values = [source.model_dump() for source in parsed_sources]
    if existing is not None:
        old_sources = [source.model_dump() for source in existing.frontmatter.sources]
        source_values = old_sources + source_values
    job = session.get(IngestJob, item.job_id) if item.job_id is not None else None
    if job is not None and not any(source["document_id"] == job.document_id for source in source_values):
        source_values.append(WikiSource(document_id=job.document_id).model_dump())
    merged_sources: dict[int, dict[str, Any]] = {}
    for source in source_values:
        document_id = int(source["document_id"])
        current = merged_sources.setdefault(
            document_id,
            {"document_id": document_id, "chunk_ids": [], "clauses": []},
        )
        current["chunk_ids"] = list(
            dict.fromkeys([*current["chunk_ids"], *(source.get("chunk_ids") or [])])
        )
        current["clauses"] = list(
            dict.fromkeys([*current["clauses"], *(source.get("clauses") or [])])
        )
    values["sources"] = list(merged_sources.values())
    content = (item.candidate_content_md or "").strip()
    if not content:
        raise ValueError("candidate content is missing")
    try:
        page = WikiPage(frontmatter=WikiFrontmatter.model_validate(values), body=content)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Wiki candidate: {exc}") from exc
    if existing is not None and page.type != existing.page_type:
        raise ValueError("candidate page type cannot change")
    return page.page_key, page


def _best_effort_log(function: Any, **fields: Any) -> None:
    try:
        function(**fields)
    except OSError:
        # A filesystem audit log must not undo a committed DB decision.
        pass


def _best_effort_rebuild_derived(session: Session, space_id: int | None = None) -> None:
    try:
        sid = resolve_space_id(session, space_id)
        rows = session.exec(
            select(WikiPageRow)
            .where(space_scope_clause(session, WikiPageRow.space_id, sid))
            .order_by(WikiPageRow.id)
        ).all()
        rebuild_index(rows, session=session, space_id=sid)
        rebuild_overview(rows, session=session, space_id=sid)
    except OSError:
        # Derived navigation must not roll back an already committed review.
        pass


def _approve_review_item(
    session: Session,
    item: WikiReviewItem,
    sid: int,
    body: WikiReviewDecisionIn | None,
    *,
    rebuild_derived: bool = True,
) -> WikiReviewDetailOut:
    review_id = int(item.id or 0)
    _review_scope(session, item, sid)
    if item.status != "pending":
        raise HTTPException(status_code=409, detail="Only pending review items can be approved")
    item.space_id = item.space_id or sid
    if item.kind == "merge":
        raise HTTPException(status_code=409, detail="Merge candidates require manual review")

    payload = _json_object(item.payload_json)
    operation = str(payload.get("operation") or ("update" if item.page_id else "create"))
    if operation not in {"create", "update"}:
        raise HTTPException(
            status_code=422,
            detail="Review item has no applicable create/update candidate",
        )
    try:
        page_key, page = _candidate_page(session, item, operation)
    except (ValueError, WikiPageNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    decision_reason = (
        (body.decision_reason if body else None)
        or (body.reason if body else None)
        or item.reason
    ).strip()
    with page_key_lock(f"review.item.{review_id}", sid), page_key_lock(page_key, sid):
        session.refresh(item)
        if item.status != "pending":
            raise HTTPException(
                status_code=409,
                detail="Only pending review items can be approved",
            )
        try:
            if operation == "create":
                if item.page_id is not None:
                    raise ValueError("create candidate cannot target an existing page")
                record = WikiRepository(session, space_id=sid).create(
                    page, job_id=item.job_id, reason=decision_reason
                )
                item.page_id = record.id
            else:
                if item.page_id is None:
                    raise ValueError("update candidate requires page_id")
                record = WikiRepository(session, space_id=sid).update(
                    page_key, page, job_id=item.job_id, reason=decision_reason
                )
        except (ValueError, WikiPageAlreadyExistsError, WikiPageNotFoundError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        item.status = "approved"
        item.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        item.reviewed_by = body.reviewed_by if body else None
        item.decision_reason = decision_reason
        session.add(item)
        session.commit()
    _best_effort_log(
        log_review,
        review_id=review_id,
        action="approve",
        page_id=record.id,
        job_id=item.job_id,
        revision=record.revision,
        reason=decision_reason,
    )
    if rebuild_derived:
        _best_effort_rebuild_derived(session, sid)
    return _review_detail(session, item)


def _acknowledge_review_item(
    session: Session,
    item: WikiReviewItem,
    sid: int,
    body: WikiReviewDecisionIn | None,
) -> WikiReviewDetailOut:
    review_id = int(item.id or 0)
    _review_scope(session, item, sid)
    if item.status != "pending":
        raise HTTPException(
            status_code=409,
            detail="Only pending review items can be acknowledged",
        )
    payload = _json_object(item.payload_json)
    operation = str(payload.get("operation") or "")
    if operation in {"create", "update", "merge"} or item.candidate_content_md:
        raise HTTPException(
            status_code=409,
            detail="Writable candidates must be approved or rejected, not acknowledged",
        )
    decision_reason = (
        (body.decision_reason if body else None)
        or (body.reason if body else None)
        or "已确认结构化提醒"
    ).strip()
    with page_key_lock(f"review.item.{review_id}", sid):
        session.refresh(item)
        if item.status != "pending":
            raise HTTPException(
                status_code=409,
                detail="Only pending review items can be acknowledged",
            )
        item.status = "acknowledged"
        item.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        item.reviewed_by = body.reviewed_by if body else None
        item.decision_reason = decision_reason
        session.add(item)
        session.commit()
    _best_effort_log(
        log_review,
        review_id=review_id,
        action="acknowledge",
        reason=decision_reason,
    )
    return _review_detail(session, item)


@router.get("/api/ingest-jobs", response_model=list[IngestJobOut])
def list_ingest_jobs(
    status: Optional[str] = None,
    document_id: Optional[int] = None,
    space_id: Optional[int] = None,
    session: Session = Depends(get_session),
) -> list[IngestJobOut]:
    statement = select(IngestJob).order_by(IngestJob.id.desc())
    if status:
        statuses = [item.strip() for item in status.split(",") if item.strip()]
        if statuses:
            statement = statement.where(IngestJob.status.in_(statuses))
    if document_id is not None:
        statement = statement.where(IngestJob.document_id == document_id)
    if space_id is None:
        space_id = resolve_space_id(session)
    statement = statement.where(space_scope_clause(session, IngestJob.space_id, space_id))
    rows = list(session.exec(statement).all())
    for row in rows:
        _job_space(session, row)
    return [_job_out(session, row) for row in rows]


@router.get("/api/ingest-jobs/{job_id}", response_model=IngestJobOut)
def get_ingest_job(
    job_id: int,
    space_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
) -> IngestJobOut:
    job = session.get(IngestJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    _job_scope(session, job, resolve_space_id(session, space_id))
    return _job_out(session, job)


@router.post("/api/ingest-jobs/{job_id}/cancel", response_model=IngestJobOut)
def cancel_ingest(
    job_id: int,
    space_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
) -> IngestJobOut:
    existing = session.get(IngestJob, job_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    _job_scope(session, existing, resolve_space_id(session, space_id))
    job = cancel_ingest_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    return _job_out(session, job)


@router.post("/api/ingest-jobs/{job_id}/retry-failed-windows", response_model=IngestJobOut)
def retry_ingest_failed_windows(
    job_id: int,
    space_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
) -> IngestJobOut:
    existing = session.get(IngestJob, job_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    _job_scope(session, existing, resolve_space_id(session, space_id))
    try:
        job = retry_failed_windows(session, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    return _job_out(session, job)


@router.get("/api/wiki/pages", response_model=List[WikiPageOut])
def list_wiki_pages(
    space_id: Optional[int] = Query(default=None),
    include_archived: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> list[WikiPageOut]:
    sid = resolve_space_id(session, space_id)
    statement = select(WikiPageRow).where(space_scope_clause(session, WikiPageRow.space_id, sid))
    if not include_archived:
        statement = statement.where(WikiPageRow.status != "archived")
    rows = session.exec(statement.order_by(WikiPageRow.id.desc())).all()
    return [_to_page_out(r, include_content=False, session=session) for r in rows]


@router.get("/api/wiki/pages/{page_id}", response_model=WikiPageOut)
def get_wiki_page(
    page_id: int,
    space_id: Optional[int] = Query(default=None),
    include_archived: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> WikiPageOut:
    row = session.get(WikiPageRow, page_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    _page_scope(session, row, resolve_space_id(session, space_id))
    if row.status == "archived" and not include_archived:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    return _to_page_out(row, include_content=True, session=session)


@router.get("/api/wiki/reviews", response_model=list[WikiReviewOut])
def list_wiki_reviews(
    status: Optional[str] = None,
    kind: Optional[str] = None,
    page_id: Optional[int] = None,
    job_id: Optional[int] = None,
    space_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
) -> list[WikiReviewOut]:
    statement = select(WikiReviewItem).order_by(WikiReviewItem.id.desc())
    if status is not None:
        statement = statement.where(WikiReviewItem.status == status)
    if kind is not None:
        statement = statement.where(WikiReviewItem.kind == kind)
    if page_id is not None:
        statement = statement.where(WikiReviewItem.page_id == page_id)
    if job_id is not None:
        statement = statement.where(WikiReviewItem.job_id == job_id)
    sid = resolve_space_id(session, space_id)
    statement = statement.where(space_scope_clause(session, WikiReviewItem.space_id, sid))
    return [_review_summary(item, session) for item in session.exec(statement).all()]


@router.get("/api/wiki/reviews/{review_id}", response_model=WikiReviewDetailOut)
def get_wiki_review(
    review_id: int,
    space_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
) -> WikiReviewDetailOut:
    item = session.get(WikiReviewItem, review_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Wiki review item not found")
    _review_scope(session, item, resolve_space_id(session, space_id))
    return _review_detail(session, item)


@router.post("/api/wiki/reviews/{review_id}/approve", response_model=WikiReviewDetailOut)
def approve_wiki_review(
    review_id: int,
    body: WikiReviewDecisionIn | None = None,
    space_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
) -> WikiReviewDetailOut:
    item = session.get(WikiReviewItem, review_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Wiki review item not found")
    sid = resolve_space_id(session, space_id)
    return _approve_review_item(session, item, sid, body)


@router.post("/api/wiki/reviews/{review_id}/reject", response_model=WikiReviewDetailOut)
def reject_wiki_review(
    review_id: int,
    body: WikiReviewDecisionIn | None = None,
    space_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
) -> WikiReviewDetailOut:
    item = session.get(WikiReviewItem, review_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Wiki review item not found")
    sid = resolve_space_id(session, space_id)
    _review_scope(session, item, sid)
    with page_key_lock(f"review.item.{review_id}", sid):
        session.refresh(item)
        if item.status != "pending":
            raise HTTPException(
                status_code=409,
                detail="Only pending review items can be rejected",
            )
        decision_reason = (
            (body.decision_reason if body else None)
            or (body.reason if body else None)
            or "rejected"
        ).strip()
        item.status = "rejected"
        item.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        item.reviewed_by = body.reviewed_by if body else None
        item.decision_reason = decision_reason
        session.add(item)
        session.commit()
    _best_effort_log(log_review, review_id=review_id, action="reject", reason=decision_reason)
    return _review_detail(session, item)


@router.post("/api/wiki/reviews/{review_id}/acknowledge", response_model=WikiReviewDetailOut)
def acknowledge_wiki_review(
    review_id: int,
    body: WikiReviewDecisionIn | None = None,
    space_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
) -> WikiReviewDetailOut:
    """Close a structural reminder that has no page candidate to approve."""

    item = session.get(WikiReviewItem, review_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Wiki review item not found")
    sid = resolve_space_id(session, space_id)
    return _acknowledge_review_item(session, item, sid, body)


@router.post("/api/wiki/reviews/batch-approve", response_model=WikiReviewBatchOut)
def batch_approve_wiki_reviews(
    body: WikiReviewBatchIn,
    space_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
) -> WikiReviewBatchOut:
    """Approve safe page candidates and acknowledge non-writable reminders.

    Every item is isolated: merge candidates, malformed candidates and stale
    rows remain pending and are reported instead of aborting the batch.
    """

    sid = resolve_space_id(session, space_id)
    decision = WikiReviewDecisionIn(
        reviewed_by=body.reviewed_by,
        decision_reason=body.decision_reason or "批量审核通过",
    )
    approved_ids: list[int] = []
    acknowledged_ids: list[int] = []
    skipped: list[WikiReviewBatchSkipOut] = []
    for review_id in dict.fromkeys(body.review_ids):
        item = session.get(WikiReviewItem, review_id)
        if item is None:
            skipped.append(
                WikiReviewBatchSkipOut(review_id=review_id, reason="审核项不存在")
            )
            continue
        try:
            _review_scope(session, item, sid)
            if item.status != "pending":
                raise HTTPException(status_code=409, detail="审核项已被处理")
            payload = _json_object(item.payload_json)
            operation = str(payload.get("operation") or "")
            if item.kind == "merge" or operation == "merge":
                raise HTTPException(status_code=409, detail="合并候选需要人工处理")
            if operation in {"create", "update"} or item.candidate_content_md:
                _approve_review_item(
                    session,
                    item,
                    sid,
                    decision,
                    rebuild_derived=False,
                )
                approved_ids.append(review_id)
            else:
                _acknowledge_review_item(session, item, sid, decision)
                acknowledged_ids.append(review_id)
        except HTTPException as exc:
            session.rollback()
            skipped.append(
                WikiReviewBatchSkipOut(
                    review_id=review_id,
                    reason=str(exc.detail),
                )
            )
        except (ValueError, WikiPageAlreadyExistsError, WikiPageNotFoundError) as exc:
            session.rollback()
            skipped.append(
                WikiReviewBatchSkipOut(review_id=review_id, reason=str(exc))
            )

    if approved_ids:
        _best_effort_rebuild_derived(session, sid)
    return WikiReviewBatchOut(
        approved_ids=approved_ids,
        acknowledged_ids=acknowledged_ids,
        skipped=skipped,
    )


@router.get("/api/wiki/pages/{page_id}/revisions", response_model=list[WikiRevisionOut])
def list_wiki_revisions(
    page_id: int,
    space_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
) -> list[WikiRevisionOut]:
    page = session.get(WikiPageRow, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    _page_scope(session, page, resolve_space_id(session, space_id))
    rows = session.exec(
        select(WikiPageRevision)
        .where(WikiPageRevision.page_id == page_id)
        .order_by(WikiPageRevision.revision)
    ).all()
    return [_revision_out(row) for row in rows]


@router.get("/api/wiki/pages/{page_id}/revisions/{revision_id}", response_model=WikiRevisionOut)
def get_wiki_revision(
    page_id: int,
    revision_id: int,
    space_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
) -> WikiRevisionOut:
    page = session.get(WikiPageRow, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    _page_scope(session, page, resolve_space_id(session, space_id))
    row = session.get(WikiPageRevision, revision_id)
    if row is None or row.page_id != page_id:
        raise HTTPException(status_code=404, detail="Wiki revision not found")
    return _revision_out(row)


@router.get("/api/wiki/pages/{page_id}/diff", response_model=WikiDiffOut)
def get_wiki_diff(
    page_id: int,
    from_revision: Optional[int] = None,
    to_revision: Optional[int] = None,
    space_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
) -> WikiDiffOut:
    page = session.get(WikiPageRow, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    _page_scope(session, page, resolve_space_id(session, space_id))
    rows = session.exec(
        select(WikiPageRevision)
        .where(WikiPageRevision.page_id == page_id)
        .order_by(WikiPageRevision.revision)
    ).all()
    if not rows:
        raise HTTPException(status_code=404, detail="Wiki revision not found")
    before = _revision_for_page(session, page_id, from_revision) if from_revision else (rows[-2] if len(rows) > 1 else rows[0])
    after = _revision_for_page(session, page_id, to_revision) if to_revision else rows[-1]
    if before is None or after is None:
        raise HTTPException(status_code=404, detail="Wiki revision not found")
    unified = "".join(
        difflib.unified_diff(
            (before.content_md or "").splitlines(True),
            (after.content_md or "").splitlines(True),
            fromfile=f"revision-{before.revision}",
            tofile=f"revision-{after.revision}",
        )
    )
    return WikiDiffOut(
        from_revision=before.revision,
        to_revision=after.revision,
        unified=unified,
        text=unified,
        changed=before.content_md != after.content_md,
        available=True,
    )


@router.post("/api/wiki/pages/{page_id}/rollback", response_model=WikiRollbackOut)
def rollback_wiki_page(
    page_id: int,
    body: WikiRollbackIn,
    space_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
) -> WikiRollbackOut:
    row = session.get(WikiPageRow, page_id)
    if row is None or not row.page_key:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    sid = resolve_space_id(session, space_id)
    _page_scope(session, row, sid)
    target_number = body.revision
    target = session.get(WikiPageRevision, body.revision_id) if body.revision_id else None
    if (target is None or target.page_id != page_id) and body.revision_id is not None:
        target = _revision_for_page(session, page_id, body.revision_id)
    if target is None and target_number is not None:
        target = _revision_for_page(session, page_id, target_number)
    if target is None or target.page_id != page_id:
        raise HTTPException(status_code=404, detail="Wiki revision not found")
    if body.job_id is not None and session.get(IngestJob, body.job_id) is None:
        raise HTTPException(status_code=422, detail="Rollback job does not exist")
    metadata = _json_object(target.frontmatter_json)
    try:
        page = WikiPage(
            frontmatter=WikiFrontmatter.model_validate(metadata),
            body=target.content_md or "",
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Historical revision is invalid: {exc}") from exc
    try:
        record = WikiRepository(session, space_id=sid).rollback(
            row.page_key,
            page,
            job_id=body.job_id,
            reason=body.reason,
        )
    except (ValueError, WikiPageNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    created = session.exec(
        select(WikiPageRevision)
        .where(WikiPageRevision.page_id == page_id, WikiPageRevision.revision == record.revision)
    ).first()
    if created is None:
        raise HTTPException(status_code=500, detail="Rollback revision was not recorded")
    _best_effort_log(
        log_rollback,
        page_id=page_id,
        from_revision=target.revision,
        to_revision=created.revision,
        job_id=body.job_id,
        reason=body.reason,
        reviewed_by=body.reviewed_by,
    )
    _best_effort_rebuild_derived(session, sid)
    return WikiRollbackOut(**_revision_out(created).model_dump())


@router.get("/api/source-chunks/{chunk_id}", response_model=SourceChunkOut)
def get_source_chunk(
    chunk_id: int,
    space_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
) -> SourceChunk:
    row = session.get(SourceChunk, chunk_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Source chunk not found")
    sid = resolve_space_id(session, space_id)
    default_space_id = resolve_space_id(session)
    actual_space_id = int(row.space_id) if row.space_id is not None else default_space_id
    if actual_space_id != sid:
        raise HTTPException(status_code=404, detail="Source chunk not found in this space")
    if row.space_id is None:
        row.space_id = sid
    return row


@router.get("/api/wiki/index", response_model=WikiIndexOut)
def get_wiki_index(
    space_id: Optional[int] = Query(default=None),
    session: Session = Depends(get_session),
) -> WikiIndexOut:
    config.ensure_data_dirs()
    space = resolve_space(session, space_id)
    sid = int(space.id or 0)
    rows = session.exec(
        select(WikiPageRow)
        .where(space_scope_clause(session, WikiPageRow.space_id, sid))
        .order_by(WikiPageRow.id)
    ).all()
    content = rebuild_index(rows, session=session, space_id=sid)
    return WikiIndexOut(content=content, path=f"wiki/spaces/{space.slug}/index.md")


@router.post("/api/wiki/retrieve", response_model=RetrieveResponse)
def retrieve_wiki(
    body: RetrieveRequest,
    session: Session = Depends(get_session),
) -> RetrieveResponse:
    """Hybrid retrieve: Wiki + source chunks + clause anchors."""
    top_k = body.top_k if body.top_k is not None else RETRIEVE_TOP_K
    wiki_k = min(RETRIEVE_WIKI_TOP_K, max(1, top_k // 2 + top_k % 2))
    source_k = min(RETRIEVE_SOURCE_TOP_K, max(1, top_k - wiki_k))

    from app.services.hybrid_retrieve import hybrid_retrieve

    result = hybrid_retrieve(
        session,
        body.query,
        top_k=top_k,
        wiki_k=wiki_k,
        source_k=source_k,
        types=body.types,
        space_id=resolve_space_id(session, body.space_id),
    )

    hits: list[RetrieveHit] = []
    for h in result["hits"]:
        hits.append(
            RetrieveHit(
                id=h.get("id"),
                title=h.get("title") or "",
                page_type=h.get("page_type") or "",
                path=h.get("path") or "",
                score=float(h.get("score") or 0),
                snippet=h.get("snippet") or "",
                tags=list(h.get("tags") or []),
                content=h.get("content") if h.get("citation_type") == "source" else None,
                source_document_id=h.get("source_document_id") or h.get("document_id"),
                citation_type=h.get("citation_type") or "wiki",
                source_chunk_id=h.get("source_chunk_id") or (
                    h.get("id") if h.get("citation_type") == "source" else None
                ),
                start_char=h.get("start_char"),
                end_char=h.get("end_char"),
                  clause_ids=list(h.get("clause_ids") or []),
                  anchor_clause=h.get("anchor_clause"),
                  explain=h.get("explain"),
                  page_key=h.get("page_key"),
                  domain=h.get("domain"),
                  status=h.get("status"),
                  revision=h.get("revision"),
                  aliases=list(h.get("aliases") or []),
                  source_document_ids=list(h.get("source_document_ids") or []),
                  space_id=h.get("space_id"),
            )
        )
    return RetrieveResponse(
        query=body.query,
        hits=hits,
        wiki_hit_count=int(result.get("wiki_hit_count") or 0),
        source_hit_count=int(result.get("source_hit_count") or 0),
        clause_ids=list(result.get("clause_ids") or []),
          anchored_clause_ids=[
              c for c in (result.get("anchored_clause_ids") or []) if c
          ],
          retrieval_mode=result.get("retrieval_mode"),
          explain=result.get("explain"),
      )
