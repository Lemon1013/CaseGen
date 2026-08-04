from __future__ import annotations

import json
import difflib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app import config
from app.config import RETRIEVE_SOURCE_TOP_K, RETRIEVE_TOP_K, RETRIEVE_WIKI_TOP_K
from app.db import get_session
from app.models.entities import (
    IngestJob,
    SourceChunk,
    WikiPageRevision,
    WikiPageRow,
    WikiReviewItem,
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
    WikiReviewDetailOut,
    WikiReviewOut,
    WikiReviewReasonOut,
    WikiRevisionOut,
    WikiRollbackIn,
    WikiRollbackOut,
    WikiSourceEvidenceOut,
)
from app.services.retrieve import load_all_wiki_pages, rank_pages
from app.services.source_chunks_store import load_all_source_chunks, rank_source_chunks
from app.services.wiki_jobs import cancel_ingest_job
from app.services.wiki_index import rebuild_index
from app.services.wiki_log import log_review, log_rollback
from app.services.wiki_overview import rebuild_overview
from app.services.wiki_repository import (
    WikiPageAlreadyExistsError,
    WikiPageNotFoundError,
    WikiRepository,
    page_key_lock,
)
from app.services.wiki_schema import WikiFrontmatter, WikiPage, WikiSource

router = APIRouter(tags=["wiki"])


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


def _to_page_out(row: WikiPageRow, *, include_content: bool = False) -> WikiPageOut:
    return WikiPageOut(
        id=row.id,
        path=row.path,
        title=row.title,
        page_type=row.page_type,
        source_document_id=row.source_document_id,
        page_key=row.page_key,
        domain=row.domain,
        status=row.status,
        revision=row.revision,
        aliases=_aliases_from_row(row),
        tags=_tags_from_row(row),
        content=_read_page_content(row) if include_content else None,
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


def _review_summary(item: WikiReviewItem) -> WikiReviewOut:
    return WikiReviewOut(
        id=int(item.id or 0),
        page_id=item.page_id,
        job_id=item.job_id,
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
    unified = "".join(
        difflib.unified_diff(
            old_content.splitlines(True),
            new_content.splitlines(True),
            fromfile="old Wiki revision",
            tofile="candidate Wiki revision",
        )
    )
    operation = payload.get("operation")
    risks = payload.get("risk_flags")
    if isinstance(risks, str):
        risks = [risks]
    if not isinstance(risks, list):
        risks = []
    reason_detail = WikiReviewReasonOut(
        summary=item.reason,
        kind=item.kind,
        operation=str(operation) if operation else None,
        page_key=page_key,
        risk_flags=[str(value) for value in risks],
    )
    return WikiReviewDetailOut(
        **_review_summary(item).model_dump(),
        old_version=old_out,
        new_candidate=candidate,
        reason_detail=reason_detail,
        payload=payload,
        source_evidence=sources,
        diff=WikiDiffOut(
            from_revision=old_out.revision if old_out else None,
            to_revision=None,
            unified=unified,
            text=unified,
            changed=old_content != new_content,
        ),
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
        existing = WikiRepository(session).read(row.page_key)
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


def _best_effort_rebuild_derived(session: Session) -> None:
    try:
        rows = session.exec(select(WikiPageRow).order_by(WikiPageRow.id)).all()
        rebuild_index(rows, session=session)
        rebuild_overview(rows, session=session)
    except OSError:
        # Derived navigation must not roll back an already committed review.
        pass


@router.get("/api/ingest-jobs", response_model=list[IngestJobOut])
def list_ingest_jobs(
    status: Optional[str] = None,
    document_id: Optional[int] = None,
    session: Session = Depends(get_session),
) -> list[IngestJob]:
    statement = select(IngestJob).order_by(IngestJob.id.desc())
    if status:
        statuses = [item.strip() for item in status.split(",") if item.strip()]
        if statuses:
            statement = statement.where(IngestJob.status.in_(statuses))
    if document_id is not None:
        statement = statement.where(IngestJob.document_id == document_id)
    return list(session.exec(statement).all())


@router.get("/api/ingest-jobs/{job_id}", response_model=IngestJobOut)
def get_ingest_job(job_id: int, session: Session = Depends(get_session)) -> IngestJob:
    job = session.get(IngestJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    return job


@router.post("/api/ingest-jobs/{job_id}/cancel", response_model=IngestJobOut)
def cancel_ingest(job_id: int, session: Session = Depends(get_session)) -> IngestJob:
    job = cancel_ingest_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Ingest job not found")
    return job


@router.get("/api/wiki/pages", response_model=List[WikiPageOut])
def list_wiki_pages(session: Session = Depends(get_session)) -> list[WikiPageOut]:
    rows = session.exec(select(WikiPageRow).order_by(WikiPageRow.id.desc())).all()
    return [_to_page_out(r, include_content=False) for r in rows]


@router.get("/api/wiki/pages/{page_id}", response_model=WikiPageOut)
def get_wiki_page(page_id: int, session: Session = Depends(get_session)) -> WikiPageOut:
    row = session.get(WikiPageRow, page_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    return _to_page_out(row, include_content=True)


@router.get("/api/wiki/reviews", response_model=list[WikiReviewOut])
def list_wiki_reviews(
    status: Optional[str] = None,
    kind: Optional[str] = None,
    page_id: Optional[int] = None,
    job_id: Optional[int] = None,
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
    return [_review_summary(item) for item in session.exec(statement).all()]


@router.get("/api/wiki/reviews/{review_id}", response_model=WikiReviewDetailOut)
def get_wiki_review(
    review_id: int, session: Session = Depends(get_session)
) -> WikiReviewDetailOut:
    item = session.get(WikiReviewItem, review_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Wiki review item not found")
    return _review_detail(session, item)


@router.post("/api/wiki/reviews/{review_id}/approve", response_model=WikiReviewDetailOut)
def approve_wiki_review(
    review_id: int,
    body: WikiReviewDecisionIn | None = None,
    session: Session = Depends(get_session),
) -> WikiReviewDetailOut:
    item = session.get(WikiReviewItem, review_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Wiki review item not found")
    if item.status != "pending":
        raise HTTPException(status_code=409, detail="Only pending review items can be approved")
    if item.kind == "merge":
        raise HTTPException(status_code=409, detail="Merge candidates require manual review")

    payload = _json_object(item.payload_json)
    operation = str(payload.get("operation") or ("update" if item.page_id else "create"))
    if operation not in {"create", "update"}:
        raise HTTPException(status_code=422, detail="Review item has no applicable create/update candidate")
    try:
        page_key, page = _candidate_page(session, item, operation)
    except (ValueError, WikiPageNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    decision_reason = ((body.decision_reason if body else None) or (body.reason if body else None) or item.reason).strip()
    with page_key_lock(f"review.item.{review_id}"), page_key_lock(page_key):
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
                record = WikiRepository(session).create(
                    page, job_id=item.job_id, reason=decision_reason
                )
                item.page_id = record.id
            else:
                if item.page_id is None:
                    raise ValueError("update candidate requires page_id")
                record = WikiRepository(session).update(
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
    _best_effort_rebuild_derived(session)
    return _review_detail(session, item)


@router.post("/api/wiki/reviews/{review_id}/reject", response_model=WikiReviewDetailOut)
def reject_wiki_review(
    review_id: int,
    body: WikiReviewDecisionIn | None = None,
    session: Session = Depends(get_session),
) -> WikiReviewDetailOut:
    item = session.get(WikiReviewItem, review_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Wiki review item not found")
    with page_key_lock(f"review.item.{review_id}"):
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


@router.get("/api/wiki/pages/{page_id}/revisions", response_model=list[WikiRevisionOut])
def list_wiki_revisions(
    page_id: int, session: Session = Depends(get_session)
) -> list[WikiRevisionOut]:
    if session.get(WikiPageRow, page_id) is None:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    rows = session.exec(
        select(WikiPageRevision)
        .where(WikiPageRevision.page_id == page_id)
        .order_by(WikiPageRevision.revision)
    ).all()
    return [_revision_out(row) for row in rows]


@router.get("/api/wiki/pages/{page_id}/revisions/{revision_id}", response_model=WikiRevisionOut)
def get_wiki_revision(
    page_id: int, revision_id: int, session: Session = Depends(get_session)
) -> WikiRevisionOut:
    if session.get(WikiPageRow, page_id) is None:
        raise HTTPException(status_code=404, detail="Wiki page not found")
    row = session.get(WikiPageRevision, revision_id)
    if row is None or row.page_id != page_id:
        raise HTTPException(status_code=404, detail="Wiki revision not found")
    return _revision_out(row)


@router.get("/api/wiki/pages/{page_id}/diff", response_model=WikiDiffOut)
def get_wiki_diff(
    page_id: int,
    from_revision: Optional[int] = None,
    to_revision: Optional[int] = None,
    session: Session = Depends(get_session),
) -> WikiDiffOut:
    if session.get(WikiPageRow, page_id) is None:
        raise HTTPException(status_code=404, detail="Wiki page not found")
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
    )


@router.post("/api/wiki/pages/{page_id}/rollback", response_model=WikiRollbackOut)
def rollback_wiki_page(
    page_id: int,
    body: WikiRollbackIn,
    session: Session = Depends(get_session),
) -> WikiRollbackOut:
    row = session.get(WikiPageRow, page_id)
    if row is None or not row.page_key:
        raise HTTPException(status_code=404, detail="Wiki page not found")
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
        record = WikiRepository(session).rollback(
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
    _best_effort_rebuild_derived(session)
    return WikiRollbackOut(**_revision_out(created).model_dump())


@router.get("/api/source-chunks/{chunk_id}", response_model=SourceChunkOut)
def get_source_chunk(
    chunk_id: int, session: Session = Depends(get_session)
) -> SourceChunk:
    row = session.get(SourceChunk, chunk_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Source chunk not found")
    return row


@router.get("/api/wiki/index", response_model=WikiIndexOut)
def get_wiki_index() -> WikiIndexOut:
    config.ensure_data_dirs()
    index_path = config.WIKI_DIR / "index.md"
    if not index_path.exists():
        content = "# Wiki Index\n\n"
    else:
        content = index_path.read_text(encoding="utf-8", errors="replace")
    return WikiIndexOut(content=content, path="wiki/index.md")


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
