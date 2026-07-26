"""Hybrid retrieve: Wiki pages + source chunks + clause anchors."""

from __future__ import annotations

from typing import Any, Optional

from sqlmodel import Session

from app import config
from app.services.clause_index import (
    build_clause_index,
    clauses_from_texts,
    extract_clause_ids,
    lookup_clauses,
)
from app.services.retrieve import load_all_wiki_pages, rank_pages
from app.services.source_chunks_store import load_all_source_chunks, rank_source_chunks


def _attach_clause_meta(chunk: dict[str, Any]) -> dict[str, Any]:
    item = dict(chunk)
    if "clause_ids" not in item:
        from app.services.clause_index import clauses_in_chunk

        item["clause_ids"] = clauses_in_chunk(item)
    # Prefer anchor in title display
    anchor = item.get("anchor_clause")
    if anchor and anchor not in (item.get("title") or ""):
        title = item.get("title") or ""
        item["title"] = f"{anchor} · {title}" if title else str(anchor)
    return item


def merge_source_hits(
    ranked: list[dict[str, Any]],
    anchored: list[dict[str, Any]],
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    """Union keyword hits with clause-anchored chunks; anchors get score floor."""
    by_id: dict[Any, dict[str, Any]] = {}
    order: list[Any] = []

    def _key(ch: dict[str, Any]) -> Any:
        return ch.get("id") if ch.get("id") is not None else id(ch)

    for ch in ranked:
        item = _attach_clause_meta(ch)
        k = _key(item)
        by_id[k] = item
        order.append(k)

    for ch in anchored:
        item = _attach_clause_meta(ch)
        # Ensure clause anchors outrank weak keyword noise when relevant
        base = float(item.get("score") or 0.0)
        item["score"] = max(base, 200.0)
        item["anchor_source"] = "clause"
        k = _key(item)
        if k in by_id:
            # keep higher score, merge meta
            prev = by_id[k]
            prev["score"] = max(float(prev.get("score") or 0), float(item["score"]))
            if item.get("anchor_clause"):
                prev["anchor_clause"] = item["anchor_clause"]
            prev["title"] = item.get("title") or prev.get("title")
            prev["clause_ids"] = list(
                dict.fromkeys(
                    list(prev.get("clause_ids") or []) + list(item.get("clause_ids") or [])
                )
            )
            prev["anchor_source"] = "clause"
        else:
            by_id[k] = item
            order.append(k)

    merged = [by_id[k] for k in order]
    merged.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    # enrich snippet with clause list for UI
    for m in merged:
        cids = m.get("clause_ids") or []
        if cids and not (m.get("snippet") or "").startswith("条款"):
            head = "条款 " + "、".join(cids[:6])
            snip = (m.get("snippet") or m.get("text") or "")[:200]
            m["snippet"] = f"{head} | {snip}"
        text = m.get("text") or m.get("content") or ""
        m.setdefault("content_excerpt", text[:2000])
        m.setdefault("citation_type", "source")
    return merged[:top_k]


def hybrid_retrieve(
    session: Session,
    query: str,
    *,
    top_k: Optional[int] = None,
    wiki_k: Optional[int] = None,
    source_k: Optional[int] = None,
    types: Optional[list[str]] = None,
) -> dict[str, Any]:
    """
    Returns {
      query, wiki_hits, source_hits, clause_ids,
      anchored_clause_ids, hits (interleaved for API)
    }
    """
    top_k = top_k if top_k is not None else config.RETRIEVE_TOP_K
    wiki_k = wiki_k if wiki_k is not None else config.RETRIEVE_WIKI_TOP_K
    source_k = source_k if source_k is not None else config.RETRIEVE_SOURCE_TOP_K

    pages = load_all_wiki_pages(session)
    chunks = load_all_source_chunks(session)
    clause_index = build_clause_index(chunks)

    wiki_hits = rank_pages(query, pages, top_k=wiki_k, types=types)
    for h in wiki_hits:
        h["citation_type"] = "wiki"
        h["clause_ids"] = extract_clause_ids(
            f"{h.get('title') or ''}\n{h.get('content') or h.get('snippet') or ''}"
        )

    ranked_source = rank_source_chunks(query, chunks, top_k=max(source_k, source_k + 2))

    # Clauses from query + wiki page bodies (anchor Wiki → 原文)
    wiki_texts = [
        f"{h.get('title') or ''}\n{h.get('content') or h.get('snippet') or ''}"
        for h in wiki_hits
    ]
    clause_ids = clauses_from_texts(query, *wiki_texts)
    anchored = lookup_clauses(clause_index, clause_ids, max_chunks=source_k + 2)
    # score anchors lightly via keyword too
    for a in anchored:
        if "score" not in a or not a["score"]:
            from app.services.retrieve import score_text

            a["score"] = score_text(
                query,
                title=a.get("title") or "",
                content=a.get("text") or "",
                tags=a.get("tags") or ["原文"],
            )

    source_hits = merge_source_hits(ranked_source, anchored, top_k=source_k)

    # Interleaved display list
    hits: list[dict[str, Any]] = []
    for h in wiki_hits:
        hits.append(
            {
                **h,
                "citation_type": "wiki",
                "source_chunk_id": None,
            }
        )
    for h in source_hits:
        hits.append(
            {
                **h,
                "citation_type": "source",
                "page_type": h.get("page_type") or "source_chunk",
                "source_chunk_id": h.get("id"),
                "content": h.get("content_excerpt") or h.get("text"),
            }
        )
    hits.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    hits = hits[:top_k]

    return {
        "query": query,
        "wiki_hits": wiki_hits,
        "source_hits": source_hits,
        "clause_ids": clause_ids,
        "anchored_clause_ids": [
            a.get("anchor_clause") for a in anchored if a.get("anchor_clause")
        ],
        "hits": hits,
        "wiki_hit_count": len(wiki_hits),
        "source_hit_count": len(source_hits),
    }
