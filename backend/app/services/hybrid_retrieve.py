"""Hybrid retrieve: Wiki pages + source chunks + clause anchors."""

from __future__ import annotations

import re
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

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


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
        item["anchor_source"] = "explicit_query"
        item["strong_anchor"] = True
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
            prev["anchor_source"] = "explicit_query"
            prev["strong_anchor"] = True
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


def _fuse_rankings(
    fts_hits: list[dict[str, Any]],
    fallback_hits: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Fuse incomparable BM25/heuristic scores with reciprocal rank fusion."""
    entries: dict[Any, dict[str, Any]] = {}
    points: dict[Any, float] = {}
    ranks: dict[Any, dict[str, int]] = {}

    def add(items: list[dict[str, Any]], mode: str) -> None:
        for rank, raw in enumerate(items, 1):
            key = raw.get("id")
            if key is None:
                key = (raw.get("path"), raw.get("title"))
            if key not in entries:
                entries[key] = dict(raw)
            else:
                current = entries[key]
                for name, value in raw.items():
                    if value not in (None, "", [], {}):
                        current[name] = value
            points[key] = points.get(key, 0.0) + 1.0 / (60 + rank)
            ranks.setdefault(key, {})[mode] = rank

    add(fallback_hits, "heuristic")
    add(fts_hits, "fts5")
    fused: list[dict[str, Any]] = []
    for key, item in entries.items():
        item["score"] = round(points[key] * 1000, 6)
        detail = dict(item.get("explain") or {})
        item["explain"] = {
            "algorithm": "reciprocal_rank_fusion",
            "ranks": ranks[key],
            "fts": detail or None,
        }
        fused.append(item)
    fused.sort(key=lambda item: (-float(item.get("score") or 0), str(item.get("id"))))
    return fused[: max(0, limit)]


def _one_hop_expand(
    direct: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    *,
    limit: int,
    types: list[str] | None,
    space_id: int | None = None,
) -> list[dict[str, Any]]:
    """Expand direct Wiki hits once through wikilinks or shared provenance."""
    if not direct or limit <= 0:
        return direct[: max(0, limit)]
    by_key = {page.get("page_key"): page for page in pages if page.get("page_key")}
    direct_ids = {page.get("id") for page in direct}
    expanded: list[dict[str, Any]] = list(direct)
    best_expansion: dict[Any, dict[str, Any]] = {}
    for seed in direct[:3]:
        linked_keys = set(_WIKILINK_RE.findall(seed.get("content") or ""))
        source_ids = set(seed.get("source_document_ids") or [])
        if seed.get("source_document_id") is not None:
            source_ids.add(seed["source_document_id"])
        for candidate in pages:
            if space_id is not None and candidate.get("space_id") not in {None, space_id}:
                continue
            candidate_id = candidate.get("id")
            if candidate_id in direct_ids or candidate.get("status") == "archived":
                continue
            if types and candidate.get("page_type") not in types:
                continue
            candidate_sources = set(candidate.get("source_document_ids") or [])
            if candidate.get("source_document_id") is not None:
                candidate_sources.add(candidate["source_document_id"])
            reasons: list[str] = []
            if candidate.get("page_key") in linked_keys:
                reasons.append("wikilink")
            if source_ids and source_ids.intersection(candidate_sources):
                reasons.append("shared_source")
            if not reasons:
                continue
            item = dict(candidate)
            item["score"] = float(seed.get("score") or 0) * 0.35
            item["snippet"] = (candidate.get("content") or "")[:200]
            item["explain"] = {
                "algorithm": "one_hop_expansion",
                "from_page_key": seed.get("page_key"),
                "reasons": reasons,
            }
            old = best_expansion.get(candidate_id)
            if old is None or float(item["score"]) > float(old.get("score") or 0):
                best_expansion[candidate_id] = item
    expanded.extend(best_expansion.values())
    expanded.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    return expanded[:limit]


def _fts_rankings(
    session: Session,
    query: str,
    pages: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    *,
    wiki_limit: int,
    source_limit: int,
    space_id: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, str | None]:
    """Search the rebuildable FTS projection and enrich hits from canonical rows."""
    try:
        from app.services.wiki_fts import index_counts, rebuild_fts, search_source_chunks, search_wiki

        counts = index_counts(session, space_id=space_id)
        if not counts.get("available"):
            return [], [], "heuristic_fallback", counts.get("reason")
        if counts.get("wiki_pages") != len(pages) or counts.get("source_chunks") != len(chunks):
            rebuild_fts(session, pages, chunks, space_id=space_id)
        wiki_raw = search_wiki(session, query, limit=max(wiki_limit * 3, 12), space_id=space_id)
        source_raw = search_source_chunks(session, query, limit=max(source_limit * 3, 12), space_id=space_id)
        if wiki_raw.error or source_raw.error:
            return [], [], "heuristic_fallback", wiki_raw.error or source_raw.error
        page_by_id = {page.get("id"): page for page in pages}
        chunk_by_id = {chunk.get("id"): chunk for chunk in chunks}
        wiki_hits = [
            {**page_by_id.get(hit.get("id"), {}), **hit}
            for hit in wiki_raw
            if hit.get("status") != "archived"
        ]
        source_hits = [{**chunk_by_id.get(hit.get("id"), {}), **hit} for hit in source_raw]
        return wiki_hits, source_hits, "fts5_hybrid", None
    except Exception as exc:
        return [], [], "heuristic_fallback", str(exc)


def hybrid_retrieve(
    session: Session,
    query: str,
    *,
    top_k: Optional[int] = None,
    wiki_k: Optional[int] = None,
    source_k: Optional[int] = None,
    types: Optional[list[str]] = None,
    space_id: Optional[int] = None,
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

    from app.services.wiki_spaces import resolve_space_id

    resolved_space_id = resolve_space_id(session, space_id)
    all_pages = load_all_wiki_pages(session, space_id=resolved_space_id)
    pages = [page for page in all_pages if page.get("status") != "archived"]
    chunks = load_all_source_chunks(session, space_id=resolved_space_id)
    clause_index = build_clause_index(chunks)

    legacy_wiki = rank_pages(query, pages, top_k=max(wiki_k * 3, 12), types=types)
    legacy_source = rank_source_chunks(query, chunks, top_k=max(source_k * 3, 12))
    fts_wiki, fts_source, retrieval_mode, retrieval_error = _fts_rankings(
        session,
        query,
        all_pages,
        chunks,
        wiki_limit=wiki_k,
        source_limit=source_k,
        space_id=resolved_space_id,
    )
    if types:
        fts_wiki = [hit for hit in fts_wiki if hit.get("page_type") in types]
    wiki_hits = _fuse_rankings(fts_wiki, legacy_wiki, limit=max(wiki_k * 2, wiki_k))
    wiki_hits = _one_hop_expand(
        wiki_hits,
        pages,
        limit=wiki_k,
        types=types,
        space_id=resolved_space_id,
    )
    for h in wiki_hits:
        h["citation_type"] = "wiki"
        h["clause_ids"] = extract_clause_ids(
            f"{h.get('title') or ''}\n{h.get('content') or h.get('snippet') or ''}"
        )

    ranked_source = _fuse_rankings(
        fts_source,
        legacy_source,
        limit=max(source_k, source_k + 2),
    )

    # Inferred clause ids remain useful metadata, but only clause ids written
    # explicitly by the user are permitted to create a strong source anchor.
    wiki_texts = [
        f"{h.get('title') or ''}\n{h.get('content') or h.get('snippet') or ''}"
        for h in wiki_hits
    ]
    clause_ids = clauses_from_texts(query, *wiki_texts)
    explicit_clause_ids = extract_clause_ids(query)
    anchored = lookup_clauses(clause_index, explicit_clause_ids, max_chunks=source_k + 2)
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
        "retrieval_mode": retrieval_mode,
        "retrieval_error": retrieval_error,
        "explain": {
            "algorithm": "fts5_plus_heuristic_rrf" if retrieval_mode == "fts5_hybrid" else "heuristic",
            "explicit_clause_ids": explicit_clause_ids,
            "one_hop_expansion": True,
        },
    }
