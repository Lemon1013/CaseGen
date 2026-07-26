"""Persist / load / rank source chunks."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, col, select

from app.models.entities import SourceChunk
from app.services.retrieve import score_text
from app.services.source_chunking import chunk_text


def delete_chunks_for_document(session: Session, document_id: int) -> int:
    rows = session.exec(
        select(SourceChunk).where(SourceChunk.document_id == document_id)
    ).all()
    n = len(rows)
    for row in rows:
        session.delete(row)
    if rows:
        session.flush()
    return n


def replace_chunks_for_document(
    session: Session,
    document_id: int,
    text: str,
    *,
    chunk_chars: int = 1200,
    overlap_chars: int = 150,
) -> list[SourceChunk]:
    delete_chunks_for_document(session, document_id)
    built = chunk_text(text, chunk_chars=chunk_chars, overlap_chars=overlap_chars)
    rows: list[SourceChunk] = []
    for item in built:
        row = SourceChunk(
            document_id=document_id,
            chunk_index=int(item["chunk_index"]),
            title=str(item["title"] or ""),
            text=str(item["text"] or ""),
            start_char=int(item["start_char"] or 0),
            end_char=int(item["end_char"] or 0),
        )
        session.add(row)
        rows.append(row)
    session.flush()
    for row in rows:
        session.refresh(row)
    return rows


def load_all_source_chunks(session: Session) -> list[dict[str, Any]]:
    rows = session.exec(
        select(SourceChunk).order_by(
            col(SourceChunk.document_id), col(SourceChunk.chunk_index)
        )
    ).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": row.id,
                "document_id": row.document_id,
                "chunk_index": row.chunk_index,
                "title": row.title,
                "text": row.text,
                "content": row.text,
                "start_char": row.start_char,
                "end_char": row.end_char,
                "path": f"source://documents/{row.document_id}/chunks/{row.chunk_index}",
                "page_type": "source_chunk",
                "citation_type": "source",
                "tags": ["原文", "source"],
            }
        )
    return out


def rank_source_chunks(
    query: str,
    chunks: list[dict[str, Any]],
    top_k: int = 4,
) -> list[dict[str, Any]]:
    if not query.strip() or top_k <= 0:
        return []
    scored: list[dict[str, Any]] = []
    for c in chunks:
        s = score_text(
            query,
            title=c.get("title") or "",
            content=c.get("text") or c.get("content") or "",
            tags=c.get("tags") or ["原文"],
        )
        if s <= 0:
            continue
        item = dict(c)
        item["score"] = s
        text = c.get("text") or c.get("content") or ""
        item["snippet"] = text[:240]
        item["content_excerpt"] = text[:2000]
        scored.append(item)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
