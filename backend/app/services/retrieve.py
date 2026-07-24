from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app import config
from app.db import get_engine
from app.models.entities import WikiPageRow


def _tokens(text: str) -> list[str]:
    text = text.lower()
    # CJK bigrams + ascii words
    parts = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text)
    tokens: list[str] = []
    for p in parts:
        if re.fullmatch(r"[\u4e00-\u9fff]+", p):
            if len(p) == 1:
                tokens.append(p)
            else:
                tokens.extend(p[i : i + 2] for i in range(len(p) - 1))
        else:
            tokens.append(p)
    return tokens


def score_text(
    query: str,
    *,
    title: str,
    content: str,
    tags: list[str] | None = None,
) -> float:
    q_tokens = _tokens(query)
    if not q_tokens:
        return 0.0
    title_l = title.lower()
    content_l = content.lower()
    tags_l = " ".join(tags or []).lower()
    score = 0.0
    for t in q_tokens:
        if t in title_l:
            score += 10.0
        if t in tags_l:
            score += 4.0
        if t in content_l:
            score += 1.0
    return score


def rank_pages(
    query: str,
    pages: list[dict[str, Any]],
    top_k: int = 6,
    types: list[str] | None = None,
) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    scored: list[dict[str, Any]] = []
    for p in pages:
        if types and p.get("page_type") not in types:
            continue
        s = score_text(
            query,
            title=p.get("title") or "",
            content=p.get("content") or "",
            tags=p.get("tags") or [],
        )
        if s <= 0:
            continue
        item = dict(p)
        item["score"] = s
        snippet = (p.get("content") or "")[:200]
        item["snippet"] = snippet
        scored.append(item)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def load_all_wiki_pages(session: Session | None = None) -> list[dict[str, Any]]:
    """Load wiki_pages rows and attach on-disk markdown content."""

    def _read_rows(sess: Session) -> list[dict[str, Any]]:
        rows = sess.exec(select(WikiPageRow)).all()
        pages: list[dict[str, Any]] = []
        for row in rows:
            path = row.path or ""
            file_path = Path(path)
            if not file_path.is_absolute():
                # Prefer wiki root, then pages dir (path may be relative to either).
                candidate = config.WIKI_DIR / path
                if not candidate.exists():
                    candidate = config.WIKI_PAGES_DIR / path
                file_path = candidate
            content = ""
            if file_path.exists() and file_path.is_file():
                content = file_path.read_text(encoding="utf-8")
            try:
                tags = json.loads(row.tags_json or "[]")
            except json.JSONDecodeError:
                tags = []
            if not isinstance(tags, list):
                tags = []
            pages.append(
                {
                    "id": row.id,
                    "title": row.title,
                    "page_type": row.page_type,
                    "path": row.path,
                    "content": content,
                    "tags": tags,
                    "source_document_id": row.source_document_id,
                }
            )
        return pages

    if session is not None:
        return _read_rows(session)
    with Session(get_engine()) as sess:
        return _read_rows(sess)
