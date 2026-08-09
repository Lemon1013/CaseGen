from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlmodel import Session, or_, select

from app import config
from app.db import get_engine
from app.models.entities import WikiPageRow, WikiPageSource

# Instruction-y noise often appended to requirements; hurts keyword retrieve.
_QUERY_NOISE = re.compile(
    r"(生成|编写|输出|给出|请).{0,12}(正常|边界|异常|正例|反例)?.{0,8}"
    r"(测试用例|用例|案例|场景)[。．\.！!？?]*",
    re.I,
)
_CLAUSE_RE = re.compile(r"\d+(?:\.\d+){1,3}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")

# Very common connectors / document meta — low signal for ranking.
_STOP_BIGRAMS = frozenset(
    {
        "根据",
        "按照",
        "进行",
        "可以",
        "应当",
        "或者",
        "以及",
        "有关",
        "相关",
        "本所",
        "规则",
        "中的",
        "中开",
        "则中",
        "价的",
        "的成",
        "格撮",
        "合规",
        "上交",
        "交所",
        "所交",
        "易规",
        "交易",  # too common across whole SSE rulebook
    }
)


def clean_retrieve_query(query: str) -> str:
    """Drop generation-instruction tails so domain terms dominate ranking."""
    q = (query or "").strip()
    if not q:
        return ""
    q = _QUERY_NOISE.sub(" ", q)
    q = re.sub(r"\s+", " ", q).strip(" ，,。．")
    return q or query.strip()


def _clause_ids(text: str) -> list[str]:
    return _CLAUSE_RE.findall(text.lower())


def _cjk_ngrams(text: str, n: int) -> list[str]:
    out: list[str] = []
    for run in _CJK_RUN.findall(text):
        if len(run) < n:
            continue
        out.extend(run[i : i + n] for i in range(len(run) - n + 1))
    return out


def _ascii_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]{2,}", text.lower())


def _key_phrases(query: str) -> list[str]:
    """
    Prefer multi-char domain phrases over the whole noisy sentence.
    e.g. 集合竞价 / 成交价格 / 撮合规则
    """
    q = clean_retrieve_query(query)
    phrases: list[str] = []
    for run in _CJK_RUN.findall(q):
        # 4-char windows (成交价格、集合竞价、开盘集合…)
        if len(run) >= 4:
            phrases.extend(run[i : i + 4] for i in range(len(run) - 3))
        # 3-char windows (撮合规 is weak; 集合竞 / 竞价的 — still useful)
        if len(run) >= 3:
            phrases.extend(run[i : i + 3] for i in range(len(run) - 2))
    # de-dupe
    seen: set[str] = set()
    uniq: list[str] = []
    for p in phrases:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)
    return uniq


def score_text(
    query: str,
    *,
    title: str,
    content: str,
    tags: list[str] | None = None,
) -> float:
    """
    Generic CJK keyword score — no domain-specific topic boosts.

    Signals (all topic-agnostic):
    - clause ids in query (e.g. 3.5.2)
    - ascii tokens
    - CJK bigrams (with light stop-list)
    - 3/4-char phrases from the query itself
    """
    query = clean_retrieve_query(query)
    if not query:
        return 0.0

    title_l = (title or "").lower()
    content_l = (content or "").lower()
    tags_l = " ".join(tags or []).lower()

    score = 0.0

    # --- clause numbers ---
    for cid in set(_clause_ids(query)):
        if cid in title_l:
            score += 50.0
        elif cid in content_l:
            score += 35.0

    # --- ascii tokens ---
    for tok in set(_ascii_tokens(query)):
        if tok in title_l:
            score += 8.0
        elif tok in tags_l:
            score += 4.0
        elif tok in content_l:
            score += 2.0

    # --- CJK bigrams (low weight; skip stop-ish) ---
    for bg in set(_cjk_ngrams(query, 2)):
        if bg in _STOP_BIGRAMS:
            continue
        if bg in title_l:
            score += 3.0
        elif bg in tags_l:
            score += 1.5
        elif bg in content_l:
            score += 1.0

    # --- key phrases (main signal): 4-char then 3-char windows from query ---
    ph4_hits = 0
    ph3_hits = 0
    for ph in _key_phrases(query):
        if len(ph) >= 4:
            if ph in title_l:
                score += 10.0
                ph4_hits += 1
            elif ph in content_l:
                score += 14.0
                ph4_hits += 1
            if ph4_hits >= 6:
                break
        else:
            if ph3_hits >= 4:
                continue
            if ph in title_l:
                score += 4.0
                ph3_hits += 1
            elif ph in content_l:
                score += 6.0
                ph3_hits += 1

    return score


def rank_pages(
    query: str,
    pages: list[dict[str, Any]],
    top_k: int = 6,
    types: list[str] | None = None,
) -> list[dict[str, Any]]:
    if not (query or "").strip():
        return []
    scored: list[dict[str, Any]] = []
    for p in pages:
        if types and p.get("page_type") not in types:
            continue
        s = score_text(
            query,
            title=" ".join(
                [p.get("title") or "", *[str(alias) for alias in (p.get("aliases") or [])]]
            ),
            content=p.get("content") or "",
            tags=p.get("tags") or [],
        )
        if s <= 0:
            continue
        item = dict(p)
        item["score"] = s
        snippet = _query_centered_snippet(p.get("content") or "", query, 200)
        item["snippet"] = snippet
        scored.append(item)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def _query_centered_snippet(text: str, query: str, limit: int = 200) -> str:
    """Return a stable excerpt around the first useful query phrase."""
    if not text or limit <= 0:
        return ""
    terms = [*_clause_ids(query), *_key_phrases(query), *_ascii_tokens(query)]
    position = min(
        (text.lower().find(term.lower()) for term in terms if term and term.lower() in text.lower()),
        default=0,
    )
    start = max(0, position - limit // 3)
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def load_all_wiki_pages(
    session: Session | None = None,
    *,
    space_id: int | None = None,
) -> list[dict[str, Any]]:
    """Load one space's pages and attach on-disk Markdown content.

    ``space_id=None`` is a compatibility call that explicitly resolves to the
    default space; it never means "all spaces".
    """

    from app.services.wiki_spaces import resolve_space_id, space_scope_clause

    def _read_rows(sess: Session, sid: int) -> list[dict[str, Any]]:
        rows = sess.exec(select(WikiPageRow).where(space_scope_clause(sess, WikiPageRow.space_id, sid))).all()
        page_ids = [row.id for row in rows if row.id is not None]
        source_statement = select(WikiPageSource)
        if page_ids:
            source_statement = source_statement.where(WikiPageSource.page_id.in_(page_ids))
        source_rows = sess.exec(source_statement).all() if page_ids else []
        sources_by_page: dict[int, list[int]] = {}
        for source in source_rows:
            sources_by_page.setdefault(source.page_id, []).append(source.document_id)
        pages: list[dict[str, Any]] = []
        for row in rows:
            path = row.path or ""
            file_path = Path(path)
            if not file_path.is_absolute():
                candidate = config.WIKI_DIR / path
                if not candidate.exists():
                    candidate = config.WIKI_PAGES_DIR / path
                file_path = candidate
            content = ""
            try:
                safe_path = file_path.resolve()
                safe_path.relative_to(config.WIKI_DIR.resolve())
            except (OSError, ValueError):
                safe_path = None
            if safe_path is not None and safe_path.exists() and safe_path.is_file():
                content = safe_path.read_text(encoding="utf-8", errors="replace")
            try:
                tags = json.loads(row.tags_json or "[]")
            except json.JSONDecodeError:
                tags = []
            if not isinstance(tags, list):
                tags = []
            try:
                aliases = json.loads(row.aliases_json or "[]")
            except json.JSONDecodeError:
                aliases = []
            if not isinstance(aliases, list):
                aliases = []
            source_document_ids = list(dict.fromkeys(sources_by_page.get(row.id or -1, [])))
            if row.source_document_id is not None and row.source_document_id not in source_document_ids:
                source_document_ids.insert(0, row.source_document_id)
            pages.append(
                {
                    "id": row.id,
                    "title": row.title,
                    "page_type": row.page_type,
                    "path": row.path,
                    "content": content,
                    "tags": tags,
                    "aliases": aliases,
                    "page_key": row.page_key,
                    "domain": row.domain,
                    "status": row.status,
                    "revision": row.revision,
                    "source_document_id": row.source_document_id,
                    "source_document_ids": source_document_ids,
                    "space_id": row.space_id if row.space_id is not None else sid,
                }
            )
        return pages

    if session is not None:
        return _read_rows(session, resolve_space_id(session, space_id))
    with Session(get_engine()) as sess:
        return _read_rows(sess, resolve_space_id(sess, space_id))
