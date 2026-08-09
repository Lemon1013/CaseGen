"""Persist / load / rank source chunks."""

from __future__ import annotations

import json
from typing import Any

from sqlmodel import Session, col, select

from app.models.entities import Document, SourceChunk
from app.services.retrieve import score_text
from app.services.parse_document import ParsedDocument
from app.services.source_chunking import chunk_text
from app.services.wiki_spaces import resolve_space_id, space_scope_clause


def _document_space_id(session: Session, document_id: int, space_id: int | None = None) -> int:
    document = session.get(Document, document_id)
    if document is not None and document.space_id is not None:
        if space_id is not None and int(space_id) != int(document.space_id):
            raise ValueError("document does not belong to the requested Wiki space")
        return int(document.space_id)
    sid = resolve_space_id(session, space_id)
    if document is not None and document.space_id is None:
        document.space_id = sid
        session.add(document)
    return sid


def delete_chunks_for_document(
    session: Session,
    document_id: int,
    space_id: int | None = None,
) -> int:
    sid = _document_space_id(session, document_id, space_id)
    rows = session.exec(
        select(SourceChunk).where(
            SourceChunk.document_id == document_id,
            space_scope_clause(session, SourceChunk.space_id, sid),
        )
    ).all()
    n = len(rows)
    for row in rows:
        if row.id is not None:
            try:
                from app.services.wiki_fts import delete_source_chunk

                with session.begin_nested():
                    delete_source_chunk(session, row.id)
            except Exception:
                # FTS is a rebuildable projection; source rows remain canonical.
                pass
        session.delete(row)
    if rows:
        session.flush()
    return n


def replace_chunks_for_document(
    session: Session,
    document_id: int,
    text: str | ParsedDocument,
    *,
    chunk_chars: int = 1200,
    overlap_chars: int = 150,
    space_id: int | None = None,
) -> list[SourceChunk]:
    sid = _document_space_id(session, document_id, space_id)
    delete_chunks_for_document(session, document_id, sid)
    built = chunk_text(text, chunk_chars=chunk_chars, overlap_chars=overlap_chars)
    rows: list[SourceChunk] = []
    for item in built:
        row = SourceChunk(
            document_id=document_id,
            space_id=sid,
            chunk_index=int(item["chunk_index"]),
            title=str(item["title"] or ""),
            text=str(item["text"] or ""),
            start_char=int(item["start_char"] or 0),
            end_char=int(item["end_char"] or 0),
            page_start=item.get("page_start"),
            page_end=item.get("page_end"),
            section=str(item.get("section") or ""),
            clause_ids_json=str(
                item.get("clause_ids_json")
                or json.dumps(item.get("clause_ids") or [], ensure_ascii=False)
            ),
            parent_index=item.get("parent_index"),
        )
        session.add(row)
        rows.append(row)
    session.flush()
    for row in rows:
        session.refresh(row)
        try:
            from app.services.wiki_fts import upsert_source_chunk

            with session.begin_nested():
                upsert_source_chunk(session, row)
        except Exception:
            # Environments without FTS5 continue through deterministic fallback.
            pass
    return rows


def load_all_source_chunks(
    session: Session,
    *,
    space_id: int | None = None,
) -> list[dict[str, Any]]:
    sid = resolve_space_id(session, space_id)
    rows = session.exec(
        select(SourceChunk)
        .where(space_scope_clause(session, SourceChunk.space_id, sid))
        .order_by(
            col(SourceChunk.document_id), col(SourceChunk.chunk_index)
        )
    ).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": row.id,
                "document_id": row.document_id,
                "space_id": row.space_id if row.space_id is not None else sid,
                "chunk_index": row.chunk_index,
                "title": row.title,
                "text": row.text,
                "content": row.text,
                "start_char": row.start_char,
                "end_char": row.end_char,
                "page_start": row.page_start,
                "page_end": row.page_end,
                "section": row.section,
                "clause_ids_json": row.clause_ids_json,
                "clause_ids": _load_clause_ids(row.clause_ids_json),
                "parent_index": row.parent_index,
                "path": f"source://documents/{row.document_id}/chunks/{row.chunk_index}",
                "page_type": "source_chunk",
                "citation_type": "source",
                "tags": ["原文", "source"],
            }
        )
    return out


def _load_clause_ids(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


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
        from app.services.retrieve import _query_centered_snippet

        item["snippet"] = _query_centered_snippet(text, query, 240)
        item["content_excerpt"] = text[:2000]
        scored.append(item)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
