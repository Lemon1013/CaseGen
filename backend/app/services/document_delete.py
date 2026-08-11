"""Safe source-document deletion with Wiki provenance cleanup."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app import config
from app.models.entities import Document, IngestJob, WikiPageRow, WikiReviewItem
from app.services.source_chunks_store import delete_chunks_for_document
from app.services.wiki_fts import rebuild_fts
from app.services.wiki_index import rebuild_index
from app.services.wiki_overview import rebuild_overview
from app.services.wiki_repository import WikiRepository, WikiRepositoryError
from app.services.wiki_schema import WikiFrontmatter, WikiPage
from app.services.wiki_spaces import space_scope_clause


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class DocumentDeleteResult:
    document_id: int
    chunks_deleted: int = 0
    pages_archived: list[str] = field(default_factory=list)
    pages_detached: list[str] = field(default_factory=list)
    reviews_closed: int = 0
    source_file_removed: bool = False
    warnings: list[str] = field(default_factory=list)


def _source_path(document: Document) -> Path:
    stored = Path((document.stored_path or "").replace("\\", "/"))
    candidate = stored if stored.is_absolute() else config.DATA_DIR / stored
    resolved = candidate.resolve()
    try:
        resolved.relative_to(config.DATA_DIR.resolve())
    except ValueError as exc:
        raise ValueError("Document path is outside data directory") from exc
    return resolved


def _contains_document_id(value: Any, document_id: int) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"document_id", "source_document_id"}:
                try:
                    if int(child) == document_id:
                        return True
                except (TypeError, ValueError):
                    pass
            if _contains_document_id(child, document_id):
                return True
    elif isinstance(value, list):
        return any(_contains_document_id(child, document_id) for child in value)
    return False


def _review_references_document(
    item: WikiReviewItem,
    document_id: int,
    job_ids: set[int],
) -> bool:
    if item.job_id in job_ids:
        return True
    for raw in (item.candidate_frontmatter_json, item.payload_json):
        try:
            value = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if _contains_document_id(value, document_id):
            return True
    return False


def delete_document(
    session: Session,
    document: Document,
    *,
    space_id: int,
) -> DocumentDeleteResult:
    """Delete source material while preserving historical task/revision rows."""

    if document.id is None:
        raise ValueError("Document must be persisted before deletion")
    document_id = int(document.id)
    if document.status == "deleted":
        raise ValueError("Document has already been deleted")
    active_job = session.exec(
        select(IngestJob)
        .where(
            IngestJob.document_id == document_id,
            IngestJob.status.in_(["queued", "running"]),
        )
        .order_by(IngestJob.id.desc())
    ).first()
    if active_job is not None:
        raise RuntimeError("Document has an active ingest job; cancel it before deletion")

    source_path = _source_path(document)
    repository = WikiRepository(session, space_id=space_id)
    archived: list[str] = []
    detached: list[str] = []
    warnings: list[str] = []
    for row in repository.list_rows():
        page_key = row.page_key
        if not page_key:
            if row.source_document_id == document_id:
                row.status = "archived"
                row.updated_at = _utcnow()
                session.add(row)
                archived.append(f"legacy-page-{row.id}")
            continue
        try:
            record = repository.read(page_key)
        except WikiRepositoryError as exc:
            if row.source_document_id == document_id:
                row.status = "archived"
                row.updated_at = _utcnow()
                session.add(row)
                archived.append(page_key)
                warnings.append(f"{page_key} 无法读取，已仅在数据库中归档：{exc}")
            continue
        if not any(
            source.document_id == document_id
            for source in record.frontmatter.sources
        ):
            continue
        remaining_sources = []
        for source in record.frontmatter.sources:
            if source.document_id == document_id:
                continue
            linked_document = session.get(Document, source.document_id)
            if linked_document is None or linked_document.status == "deleted":
                warnings.append(
                    f"{page_key} 同时包含已失效来源 #{source.document_id}，已一并移除"
                )
                continue
            remaining_sources.append(source)
        if remaining_sources:
            values = record.frontmatter.model_dump(mode="python")
            values["sources"] = remaining_sources
            repository.update(
                page_key,
                WikiPage(
                    frontmatter=WikiFrontmatter.model_validate(values),
                    body=record.body,
                ),
                reason=f"deleted source document #{document_id}",
            )
            detached.append(page_key)
        else:
            repository.archive(
                page_key,
                reason=f"only source document #{document_id} was deleted",
            )
            archived.append(page_key)

    jobs = session.exec(
        select(IngestJob).where(IngestJob.document_id == document_id)
    ).all()
    job_ids = {int(job.id) for job in jobs if job.id is not None}
    pending_reviews = session.exec(
        select(WikiReviewItem).where(
            WikiReviewItem.status == "pending",
            space_scope_clause(session, WikiReviewItem.space_id, space_id),
        )
    ).all()
    reviews_closed = 0
    for item in pending_reviews:
        if not _review_references_document(item, document_id, job_ids):
            continue
        item.status = "rejected"
        item.reviewed_at = _utcnow()
        item.reviewed_by = "system"
        item.decision_reason = f"来源文档 #{document_id} 已删除"
        session.add(item)
        reviews_closed += 1

    chunks_deleted = delete_chunks_for_document(session, document_id, space_id)
    document.status = "deleted"
    document.error_message = None
    document.updated_at = _utcnow()
    session.add(document)
    session.commit()

    source_file_removed = False
    if source_path.exists():
        try:
            if not source_path.is_file():
                warnings.append("源路径不是普通文件，未执行物理删除")
            else:
                source_path.unlink()
                source_file_removed = True
        except OSError as exc:
            warnings.append(f"源文件物理删除失败：{exc}")

    rows = session.exec(
        select(WikiPageRow)
        .where(space_scope_clause(session, WikiPageRow.space_id, space_id))
        .order_by(WikiPageRow.id)
    ).all()
    try:
        rebuild_index(rows, session=session, space_id=space_id)
        rebuild_overview(rows, session=session, space_id=space_id)
        rebuild_fts(session, space_id=space_id)
    except Exception as exc:  # projections are rebuildable
        warnings.append(f"派生索引稍后需要重建：{exc}")

    return DocumentDeleteResult(
        document_id=document_id,
        chunks_deleted=chunks_deleted,
        pages_archived=archived,
        pages_detached=detached,
        reviews_closed=reviews_closed,
        source_file_removed=source_file_removed,
        warnings=warnings,
    )
