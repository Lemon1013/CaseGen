"""Clause-number index over source chunks (e.g. 3.5.2 → chunk).

Used to anchor Wiki summaries / free-text requirements back to verbatim
regulatory text without recompiling Wiki.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# 3.5.2 / 3.5 / 11.6 — not money like 0.01 / 0.001
_CLAUSE_RE = re.compile(r"(?<![\d.])([1-9]\d*(?:\.\d+){1,2})(?![\d.])")

# Prefer longer clause ids when sorting (3.5.2 before 3.5)
def _clause_key(cid: str) -> tuple:
    parts = tuple(int(x) for x in cid.split("."))
    return (len(parts), parts)


def extract_clause_ids(text: str) -> list[str]:
    """Return unique clause ids in document order (longer forms kept)."""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for m in _CLAUSE_RE.finditer(text):
        cid = m.group(1)
        if cid in seen:
            continue
        seen.add(cid)
        found.append(cid)
    return found


def clauses_in_chunk(chunk: dict[str, Any]) -> list[str]:
    blob = "\n".join(
        [
            str(chunk.get("title") or ""),
            str(chunk.get("text") or chunk.get("content") or ""),
        ]
    )
    return extract_clause_ids(blob)


def build_clause_index(
    chunks: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Map clause_id → list of chunk dicts (stable order)."""
    index: dict[str, list[dict[str, Any]]] = {}
    for ch in chunks:
        ids = clauses_in_chunk(ch)
        # attach for downstream display
        ch = dict(ch)
        ch["clause_ids"] = ids
        for cid in ids:
            index.setdefault(cid, []).append(ch)
    return index


def lookup_clauses(
    index: dict[str, list[dict[str, Any]]],
    clause_ids: Iterable[str],
    *,
    max_chunks: int = 6,
) -> list[dict[str, Any]]:
    """Resolve clause ids to unique chunks (prefer exact longer id)."""
    wanted = list(dict.fromkeys(clause_ids))  # unique preserve order
    # Prefer more specific ids first
    wanted_sorted = sorted(wanted, key=_clause_key, reverse=True)
    out: list[dict[str, Any]] = []
    seen_ids: set[Any] = set()
    for cid in wanted_sorted:
        for ch in index.get(cid, []):
            cid_key = ch.get("id")
            if cid_key is not None and cid_key in seen_ids:
                continue
            if cid_key is not None:
                seen_ids.add(cid_key)
            item = dict(ch)
            item["anchor_clause"] = cid
            item.setdefault("clause_ids", clauses_in_chunk(ch))
            out.append(item)
            if len(out) >= max_chunks:
                return out
    return out


def clauses_from_texts(*texts: str) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for t in texts:
        for cid in extract_clause_ids(t or ""):
            if cid not in seen:
                seen.add(cid)
                merged.append(cid)
    return merged
