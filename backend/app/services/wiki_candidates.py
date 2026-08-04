"""Recall existing Wiki pages before an incremental analysis.

The candidate layer is independent from page application. It accepts
repository records, parsed pages, or plain dictionaries and returns a small,
explainable representation for the Step A prompt. Ranking is field-aware, so
a late matching page is not lost because an earlier page happened to be listed
first.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlmodel import Session, select

from app import config
from app.models.entities import WikiPageRow, WikiPageSource
from app.services.wiki_schema import is_valid_page_key, parse_wiki_page, validate_page_key


_CLAUSE_RE = re.compile(
    r"(?<![0-9A-Za-z_])(?:第[0-9一二三四五六七八九十百千万]+(?:\.\d+)*条|[1-9]\d*(?:\.\d+){1,3})(?![0-9A-Za-z_])"
)
_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$")
_TAG_RE = re.compile(r"(?<!\w)#([\w\-\u4e00-\u9fff]{2,32})")
_IDENTIFIER_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_-]{2,}(?![A-Za-z0-9_])")
_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[\s\u3000，。；：、！？（）()【】\[\]{}<>《》“”‘’'\"`~!?,.;:/\\|]+")


class WikiCandidate(BaseModel):
    """A prompt-safe summary of one existing Wiki page."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    page_key: str
    title: str = ""
    page_type: str = ""
    domain: str | None = None
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    clauses: list[str] = Field(default_factory=list)
    summary: str = ""
    body_excerpt: str = ""
    path: str | None = None
    source_count: int = Field(default=0, ge=0)
    source_document_ids: list[int] = Field(default_factory=list)
    score: float = 0.0
    matched_fields: list[str] = Field(default_factory=list)
    matched_terms: list[str] = Field(default_factory=list)

    @field_validator("page_key")
    @classmethod
    def _valid_key(cls, value: str) -> str:
        return validate_page_key(value)

    @field_validator(
        "aliases",
        "tags",
        "entities",
        "clauses",
        "matched_fields",
        "matched_terms",
        mode="before",
    )
    @classmethod
    def _strings(cls, value: Any) -> list[str]:
        return _string_list(value)

    @field_validator("source_document_ids", mode="before")
    @classmethod
    def _document_ids(cls, value: Any) -> list[int]:
        result: list[int] = []
        for item in _list_value(value):
            if isinstance(item, bool):
                continue
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in result:
                result.append(number)
        return result


def _list_value(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)):
        return [value]
    if isinstance(value, Mapping):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def _string_list(value: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in _list_value(value):
        if item is None:
            continue
        text = str(item).strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except json.JSONDecodeError:
            return []
    return _list_value(value)


def _attr(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _page_key_for(item: Any) -> str | None:
    key = _attr(item, "page_key")
    if key and is_valid_page_key(str(key)):
        return str(key)
    row = _attr(item, "row")
    key = _attr(row, "page_key")
    if key and is_valid_page_key(str(key)):
        return str(key)
    legacy_id = _attr(item, "id") or _attr(row, "id")
    if legacy_id is not None:
        try:
            number = int(legacy_id)
        except (TypeError, ValueError):
            return None
        if number > 0:
            return f"legacy.page.{number}"
    return None


def _first_sentence(text: str) -> str:
    text = _SPACE_RE.sub(" ", text or "").strip()
    if not text:
        return ""
    match = re.search(r"[。！？.!?]", text)
    return text[: match.end() if match else 240]


def _normalise_candidate(item: Any) -> WikiCandidate | None:
    if isinstance(item, WikiCandidate):
        return item.model_copy(deep=True)
    frontmatter = _attr(item, "frontmatter")
    page = _attr(item, "page")
    if frontmatter is None and page is not None:
        frontmatter = _attr(page, "frontmatter")
    row = _attr(item, "row")
    key = _page_key_for(item)
    if not key:
        return None
    title = _attr(item, "title") or _attr(frontmatter, "title") or _attr(row, "title") or ""
    page_type = _attr(item, "page_type") or _attr(item, "type") or _attr(frontmatter, "type") or _attr(row, "page_type") or ""
    aliases = _attr(item, "aliases")
    if aliases is None:
        aliases = _attr(frontmatter, "aliases") or _json_list(_attr(row, "aliases_json"))
    tags = _attr(item, "tags")
    if tags is None:
        tags = _attr(frontmatter, "tags") or _json_list(_attr(row, "tags_json"))
    entities = _attr(item, "entities")
    if entities is None:
        entities = _attr(frontmatter, "entities")
    clauses = _attr(item, "clauses")
    if clauses is None:
        clauses = _attr(frontmatter, "clauses")
    if clauses is None:
        clauses = [
            clause
            for source in _list_value(_attr(frontmatter, "sources"))
            for clause in _list_value(_attr(source, "clauses"))
        ]
    domain = _attr(item, "domain") or _attr(frontmatter, "domain") or _attr(row, "domain")
    body = _attr(item, "body") or _attr(item, "content") or _attr(item, "body_excerpt") or ""
    if not body and page is not None:
        body = _attr(page, "body") or ""
    source_ids = _attr(item, "source_document_ids")
    if source_ids is None:
        source_ids = [
            _attr(source, "document_id")
            for source in _list_value(_attr(frontmatter, "sources"))
            if _attr(source, "document_id") is not None
        ]
    source_count = _attr(item, "source_count")
    if source_count is None:
        source_count = len(_list_value(source_ids))
    summary = _attr(item, "summary") or _first_sentence(str(body or ""))
    path = _attr(item, "path") or _attr(row, "path")
    return WikiCandidate(
        page_key=key,
        title=str(title or key),
        page_type=str(page_type or ""),
        domain=str(domain) if domain else None,
        aliases=_string_list(aliases),
        tags=_string_list(tags),
        entities=_string_list(entities),
        clauses=_string_list(clauses),
        summary=str(summary or "")[:500],
        body_excerpt=str(body or "")[:1200],
        path=str(path) if path else None,
        source_count=max(0, int(source_count or 0)),
        source_document_ids=source_ids,
    )


def _normalise_term(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "").strip()).casefold()


def _term_key(value: Any) -> str:
    return _PUNCT_RE.sub("", _normalise_term(value))


def extract_source_terms(
    text: str = "",
    *,
    filename: str = "",
    title: str = "",
    aliases: Iterable[str] | None = None,
    entities: Iterable[str] | None = None,
    clauses: Iterable[str] | None = None,
    tags: Iterable[str] | None = None,
) -> dict[str, list[str]]:
    """Extract recall hints without asking an LLM to invent identifiers."""

    source = str(text or "")
    headings = _HEADING_RE.findall(source)
    file_stem = Path(filename).stem if filename else ""
    clause_terms = _CLAUSE_RE.findall(source)
    tag_terms = _TAG_RE.findall(source)
    identifier_terms = _IDENTIFIER_RE.findall(source)
    result = {
        "title": _string_list([title, *headings[:12], file_stem]),
        "aliases": _string_list(aliases),
        "entities": _string_list([*(_list_value(entities) or []), *identifier_terms]),
        "clauses": _string_list([*clause_terms, *(_list_value(clauses) or [])]),
        "tags": _string_list([*tag_terms, *(_list_value(tags) or [])]),
    }
    return {key: [item for item in values if len(_term_key(item)) >= 2] for key, values in result.items()}


def _field_terms(terms: Mapping[str, Iterable[str]] | None) -> dict[str, list[str]]:
    keys = ("title", "aliases", "entities", "clauses", "tags")
    return {key: _string_list(terms.get(key, ()) if terms else ()) for key in keys}


def _matches(term: str, value: str) -> bool:
    needle = _term_key(term)
    haystack = _term_key(value)
    return bool(needle and haystack and needle in haystack)


def _select_with_tail(items: list[WikiCandidate], limit: int | None) -> list[WikiCandidate]:
    if limit is None or limit <= 0 or len(items) <= limit:
        return items
    # Ranking is independent of database/list order, so keep the strongest
    # candidates. Head/tail quotas belong to multi-window claim merging, not
    # to a score-sorted retrieval result where the tail is least relevant.
    return items[:limit]


def recall_wiki_candidates(
    candidates: Iterable[Any],
    *,
    query: str = "",
    text: str = "",
    filename: str = "",
    title: str = "",
    aliases: Iterable[str] | None = None,
    entities: Iterable[str] | None = None,
    clauses: Iterable[str] | None = None,
    tags: Iterable[str] | None = None,
    terms: Mapping[str, Iterable[str]] | None = None,
    limit: int | None = 80,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """Recall and rank existing pages using explainable lexical evidence."""

    extracted = extract_source_terms(
        text,
        filename=filename,
        title=title,
        aliases=aliases,
        entities=entities,
        clauses=clauses,
        tags=tags,
    )
    field_terms = _field_terms(terms or extracted)
    if query:
        field_terms["title"].append(query)
    all_terms = [term for values in field_terms.values() for term in values]
    if not all_terms:
        return []

    weighted_fields = {
        "title": (12.0, "title"),
        "aliases": (10.0, "alias"),
        "clauses": (9.0, "clause"),
        "entities": (7.0, "entity"),
        "tags": (6.0, "tag"),
    }
    ranked: list[WikiCandidate] = []
    for raw in candidates:
        candidate = _normalise_candidate(raw)
        if candidate is None:
            continue
        score = 0.0
        matched_fields: list[str] = []
        matched_terms: list[str] = []
        for field, (weight, label) in weighted_fields.items():
            values = [candidate.title] if field == "title" else getattr(candidate, field)
            for term in field_terms[field]:
                if any(_matches(term, str(value)) for value in values):
                    score += weight
                    if label not in matched_fields:
                        matched_fields.append(label)
                    if term not in matched_terms:
                        matched_terms.append(term)
        searchable_body = " ".join([candidate.page_key, candidate.title, candidate.summary, candidate.body_excerpt])
        for term in all_terms:
            if _matches(term, searchable_body):
                score += 1.0
        if score <= 0:
            continue
        ranked.append(candidate.model_copy(update={"score": round(score, 4), "matched_fields": matched_fields, "matched_terms": matched_terms}))
    ranked.sort(key=lambda item: (-item.score, item.page_key))
    chosen = _select_with_tail(ranked, top_k if top_k is not None else limit)
    return [item.model_dump(mode="json") for item in chosen]


def recall_candidates(candidates: Iterable[Any], **kwargs: Any) -> list[dict[str, Any]]:
    return recall_wiki_candidates(candidates, **kwargs)


def format_candidate_context(candidates: Iterable[Any], *, max_chars: int = 16000) -> str:
    """Build a bounded candidate summary while retaining both ends."""

    rows: list[str] = []
    for item in candidates:
        candidate = _normalise_candidate(item)
        if candidate is None:
            continue
        rows.append(
            json.dumps(
                {
                    "page_key": candidate.page_key,
                    "title": candidate.title,
                    "type": candidate.page_type,
                    "domain": candidate.domain,
                    "aliases": candidate.aliases,
                    "tags": candidate.tags,
                    "entities": candidate.entities,
                    "clauses": candidate.clauses,
                    "summary": candidate.summary,
                    "source_count": candidate.source_count,
                    "matched_fields": candidate.matched_fields,
                    "matched_terms": candidate.matched_terms,
                },
                ensure_ascii=False,
            )
        )
    if not rows:
        return "（没有召回到现有 Wiki 页面）"
    text = "\n".join(rows)
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return text[:head].rstrip() + "\n…[candidate context truncated]…\n" + text[-tail:].lstrip()


def load_wiki_candidates_from_disk(root: Path | None = None) -> list[dict[str, Any]]:
    """Load valid Markdown pages for integrations without a Session."""

    root = Path(root or config.WIKI_DIR)
    if not root.exists():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.md")):
        if any(part in {".staging", ".git"} for part in path.parts):
            continue
        if path.name in {"index.md", "purpose.md", "schema.md", "overview.md", "log.md"}:
            continue
        try:
            parsed = parse_wiki_page(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, TypeError, ValueError):
            continue
        result.append(
            WikiCandidate(
                page_key=parsed.page_key,
                title=parsed.title,
                page_type=parsed.type,
                domain=parsed.frontmatter.domain,
                aliases=parsed.frontmatter.aliases,
                tags=parsed.frontmatter.tags,
                entities=[],
                clauses=[clause for source in parsed.frontmatter.sources for clause in source.clauses],
                summary=_first_sentence(parsed.body),
                body_excerpt=parsed.body[:1200],
                path=path.relative_to(root).as_posix(),
                source_count=len(parsed.frontmatter.sources),
                source_document_ids=[source.document_id for source in parsed.frontmatter.sources],
            ).model_dump(mode="json")
        )
    return result


def recall_wiki_candidates_from_session(
    session: Session,
    *,
    text: str = "",
    filename: str = "",
    limit: int | None = 80,
    top_k: int | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Recall candidates from migrated rows without opening arbitrary paths."""

    rows = session.exec(select(WikiPageRow).order_by(WikiPageRow.id)).all()
    source_counts: dict[int, int] = {}
    source_ids: dict[int, list[int]] = {}
    source_clauses: dict[int, list[str]] = {}
    for source in session.exec(select(WikiPageSource)).all():
        if source.page_id is None:
            continue
        source_counts[source.page_id] = source_counts.get(source.page_id, 0) + 1
        source_ids.setdefault(source.page_id, []).append(source.document_id)
        for clause in _json_list(source.clauses_json):
            text = str(clause).strip()
            if text and text not in source_clauses.setdefault(source.page_id, []):
                source_clauses[source.page_id].append(text)
    candidates: list[dict[str, Any]] = []
    for row in rows:
        body = ""
        path = Path(config.WIKI_DIR) / (row.path or "")
        try:
            if path.is_file():
                body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            body = ""
        candidates.append(
            {
                "id": row.id,
                "page_key": row.page_key,
                "title": row.title,
                "page_type": row.page_type,
                "domain": row.domain,
                "aliases": _json_list(row.aliases_json),
                "tags": _json_list(row.tags_json),
                "entities": _json_list(row.aliases_json) if row.page_type == "entity" else [],
                "clauses": source_clauses.get(row.id or 0, []),
                "body": body,
                "path": row.path,
                "source_count": source_counts.get(row.id or 0, 0),
                "source_document_ids": source_ids.get(row.id or 0, []),
            }
        )
    return recall_wiki_candidates(candidates, text=text, filename=filename, limit=limit, top_k=top_k, **kwargs)


recall_from_session = recall_wiki_candidates_from_session


__all__ = [
    "WikiCandidate",
    "extract_source_terms",
    "format_candidate_context",
    "load_wiki_candidates_from_disk",
    "recall_candidates",
    "recall_from_session",
    "recall_wiki_candidates",
    "recall_wiki_candidates_from_session",
]
