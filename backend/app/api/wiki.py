from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app import config
from app.config import RETRIEVE_SOURCE_TOP_K, RETRIEVE_TOP_K, RETRIEVE_WIKI_TOP_K
from app.db import get_session
from app.models.entities import IngestJob, SourceChunk, WikiPageRow
from app.schemas.documents import SourceChunkOut
from app.schemas.wiki import (
    IngestJobOut,
    RetrieveHit,
    RetrieveRequest,
    RetrieveResponse,
    WikiIndexOut,
    WikiPageOut,
)
from app.services.retrieve import load_all_wiki_pages, rank_pages
from app.services.source_chunks_store import load_all_source_chunks, rank_source_chunks

router = APIRouter(tags=["wiki"])


def _tags_from_row(row: WikiPageRow) -> list[str]:
    try:
        tags = json.loads(row.tags_json or "[]")
    except json.JSONDecodeError:
        tags = []
    if not isinstance(tags, list):
        return []
    return [str(t) for t in tags]


def _read_page_content(row: WikiPageRow) -> str:
    path = Path(row.path or "")
    if not path.is_absolute():
        candidate = config.WIKI_DIR / (row.path or "")
        if not candidate.exists():
            candidate = config.WIKI_PAGES_DIR / Path(row.path or "").name
        path = candidate
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
        tags=_tags_from_row(row),
        content=_read_page_content(row) if include_content else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/api/ingest-jobs/{job_id}", response_model=IngestJobOut)
def get_ingest_job(job_id: int, session: Session = Depends(get_session)) -> IngestJob:
    job = session.get(IngestJob, job_id)
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
    )
