from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlmodel import Session, col, func, select

from app import config
from app.models.entities import (
    CaseDraft,
    GenerationTask,
    ModelConfig,
    PromptRevision,
    PromptTemplate,
    Requirement,
    ReviewResult,
    TaskCitation,
)
from app.services.llm import LLMError, chat_completion
from app.services.retrieve import load_all_wiki_pages, rank_pages
from app.services.source_chunks_store import load_all_source_chunks, rank_source_chunks
from app.services.review_parse import parse_review_payload
from app.services.case_management import import_cases_from_draft, utcnow
from app.services.task_events import append_event
from app.services.task_state import InvalidTransition, transition
from app.services.task_stream import task_stream
from app.services.wiki_spaces import resolve_space_id

# Optional injectable chat hooks for tests: (messages, model_cfg) -> str
# Prefer explicit chat_fn arg, then stage-specific hook, then shared pipeline hook.
_PIPELINE_CHAT_FN: Optional[Callable[..., str]] = None
_GENERATE_CHAT_FN: Optional[Callable[..., str]] = None
_REVIEW_CHAT_FN: Optional[Callable[..., str]] = None
_OPTIMIZE_CHAT_FN: Optional[Callable[..., str]] = None

ChatFn = Callable[..., Any]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _set_status(task: GenerationTask, new_status: str) -> None:
    task.status = transition(task.status, new_status)
    task.updated_at = _utcnow()


def _focus_tags(requirement: Requirement) -> list[str]:
    try:
        tags = json.loads(requirement.focus_tags_json or "[]")
    except json.JSONDecodeError:
        tags = []
    if not isinstance(tags, list):
        return []
    return [str(t) for t in tags]


def _build_query(requirement: Requirement) -> str:
    from app.services.retrieve import clean_retrieve_query

    tags = _focus_tags(requirement)
    parts = [requirement.title or "", requirement.description or ""]
    if tags:
        parts.append(" ".join(tags))
    raw = " ".join(p for p in parts if p).strip()
    return clean_retrieve_query(raw)


def _resolve_generate_prompt(session: Session, task: GenerationTask) -> tuple[str, str]:
    """Return (prompt_content, prompt_version_ref)."""
    if task.temp_prompt_content:
        return task.temp_prompt_content, "temp"

    if task.prompt_template_id is not None:
        row = session.get(PromptTemplate, task.prompt_template_id)
        if row is not None:
            if row.type != "generate":
                raise RuntimeError(
                    f"PromptTemplate id={row.id} has type={row.type!r}; expected 'generate'"
                )
            return row.content, f"id:{row.id}:v{row.version}"

    active = session.exec(
        select(PromptTemplate).where(
            PromptTemplate.type == "generate",
            PromptTemplate.is_active == True,  # noqa: E712
        ).order_by(col(PromptTemplate.updated_at).desc(), col(PromptTemplate.id).desc())
    ).first()
    if active is None:
        raise RuntimeError("No active generate prompt template found")
    return active.content, f"id:{active.id}:v{active.version}"


def _resolve_model(session: Session, task: GenerationTask) -> ModelConfig:
    if task.model_id is not None:
        row = session.get(ModelConfig, task.model_id)
        if row is None:
            raise RuntimeError(f"ModelConfig id={task.model_id} not found")
        return row
    default = session.exec(
        select(ModelConfig)
        .where(ModelConfig.is_default == True)  # noqa: E712
        .order_by(col(ModelConfig.id).desc())
    ).first()
    if default is None:
        # Fall back to any model if no default is marked.
        default = session.exec(select(ModelConfig).order_by(col(ModelConfig.id).desc())).first()
    if default is None:
        raise RuntimeError("No ModelConfig available")
    return default


def _clear_citations(session: Session, task_id: int) -> None:
    rows = session.exec(select(TaskCitation).where(TaskCitation.task_id == task_id)).all()
    for row in rows:
        session.delete(row)
    session.flush()


def _strip_yaml_frontmatter(content: str) -> str:
    """Drop leading YAML frontmatter so generate context stays lean."""
    text = content or ""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4 :].lstrip("\n")


# Used when the primary generate call hits a flaky gateway (502 etc.).
_LEAN_GENERATE_SYSTEM = (
    "你是金融/交易所测试专家。根据需求、Wiki 与原文块输出中文测试用例 Markdown。"
    "每条用例含：标题、优先级、类型、关联知识、条款号、前置条件、步骤、预期。"
    "必须引用 Wiki 编号[1]与/或原文[S1]，并写明规则条款号（如 3.5.2）；"
    "覆盖正常/边界/异常；不得编造未提供的规则；只输出用例 Markdown。"
)


def _truncate_wiki_context(pages: list[dict[str, Any]], max_chars: int) -> str:
    blocks: list[str] = []
    used = 0
    for i, page in enumerate(pages, start=1):
        title = page.get("title") or ""
        path = page.get("path") or ""
        content = _strip_yaml_frontmatter(page.get("content") or "")
        header = f"[{i}] {title} ({path})\n"
        remaining = max_chars - used - len(header)
        if remaining <= 0:
            break
        body = content if len(content) <= remaining else content[:remaining]
        block = header + body
        blocks.append(block)
        used += len(block) + 2  # account for separator
        if used >= max_chars:
            break
    return "\n\n".join(blocks)


def _truncate_source_context(chunks: list[dict[str, Any]], max_chars: int) -> str:
    blocks: list[str] = []
    used = 0
    for i, ch in enumerate(chunks, start=1):
        title = ch.get("title") or f"原文块{i}"
        path = ch.get("path") or ""
        text = ch.get("text") or ch.get("content") or ch.get("content_excerpt") or ""
        cids = ch.get("clause_ids") or []
        anchor = ch.get("anchor_clause")
        clause_note = ""
        if anchor:
            clause_note = f" 锚定条款={anchor}"
        elif cids:
            clause_note = f" 含条款={','.join(cids[:8])}"
        header = f"[S{i}] {title} ({path}){clause_note}\n"
        remaining = max_chars - used - len(header)
        if remaining <= 0:
            break
        body = text if len(text) <= remaining else text[:remaining]
        block = header + body
        blocks.append(block)
        used += len(block) + 2
        if used >= max_chars:
            break
    return "\n\n".join(blocks)


# Task-context budgets are deliberately kept here, instead of in the API layer,
# so synchronous and background generation use the same evidence contract.
_WIKI_CONTEXT_RATIO = 0.60
_SOURCE_CONTEXT_RATIO = 0.35
_INDEX_CONTEXT_RATIO = 0.05
_DEFAULT_WIKI_ITEM_CHARS = 3200
_DEFAULT_SOURCE_ITEM_CHARS = 2200


def _context_clause_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [value] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _context_content(hit: Mapping[str, Any], kind: str) -> str:
    if kind == "wiki":
        return _strip_yaml_frontmatter(
            str(hit.get("content") or hit.get("content_excerpt") or hit.get("snippet") or "")
        )
    return str(hit.get("text") or hit.get("content") or hit.get("content_excerpt") or "")


def _context_hit_key(hit: Mapping[str, Any], kind: str) -> tuple[Any, ...]:
    if kind == "wiki":
        for field in ("page_key", "id", "path"):
            value = hit.get(field)
            if value is not None and str(value):
                return (kind, field, str(value))
    else:
        for field in ("source_chunk_id", "id"):
            value = hit.get(field)
            if value is not None and str(value):
                return (kind, field, str(value))
        document_id = hit.get("document_id") or hit.get("source_document_id")
        chunk_index = hit.get("chunk_index")
        if document_id is not None or chunk_index is not None:
            return (kind, "document_chunk", str(document_id), str(chunk_index))
    return (kind, "fallback", str(hit.get("title") or ""), _context_content(hit, kind)[:160])


def _context_source_group(hit: Mapping[str, Any]) -> str:
    document_id = hit.get("document_id") or hit.get("source_document_id")
    if document_id is not None:
        return f"document:{document_id}"
    path = str(hit.get("path") or "")
    match = re.search(r"(documents/[^/]+)", path)
    return match.group(1) if match else (path or "source:unknown")


def _context_explicit_clauses(query: str) -> list[str]:
    from app.services.clause_index import extract_clause_ids

    return list(dict.fromkeys(extract_clause_ids(query or "")))


def _normalise_context_hits(
    hits: Iterable[Mapping[str, Any]] | None,
    *,
    kind: str,
    explicit_clause_ids: list[str],
) -> tuple[list[dict[str, Any]], int]:
    """Copy, normalize and de-duplicate retrieval results without mutating them."""
    normalized: list[dict[str, Any]] = []
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    raw_count = 0
    for raw in hits or []:
        if not isinstance(raw, Mapping):
            continue
        raw_count += 1
        item = dict(raw)
        item["_context_content"] = _context_content(item, kind)
        clause_ids = _context_clause_ids(item.get("clause_ids"))
        if not clause_ids:
            clause_ids = _context_clause_ids(item.get("clause_ids_json"))
        item["clause_ids"] = clause_ids

        if kind == "source":
            raw_anchor = str(item.get("anchor_clause") or "").strip()
            matching = [cid for cid in explicit_clause_ids if cid in clause_ids]
            if raw_anchor in explicit_clause_ids:
                item["anchor_clause"] = raw_anchor
                item["anchor_source"] = "explicit_query"
                item["strong_anchor"] = True
            elif matching:
                item["anchor_clause"] = matching[0]
                item["anchor_source"] = "explicit_query"
                item["strong_anchor"] = True
            else:
                # Wiki-inferred anchors are useful metadata, but never become
                # strong anchors when the user's query did not name a clause.
                item["anchor_clause"] = None
                item["anchor_source"] = None
                item["strong_anchor"] = False
            item["_context_group"] = _context_source_group(item)
        else:
            item["anchor_clause"] = None
            item["strong_anchor"] = False
            item["_context_group"] = str(
                item.get("page_key") or item.get("id") or item.get("path") or "wiki:unknown"
            )

        key = _context_hit_key(item, kind)
        previous = by_key.get(key)
        if previous is None:
            by_key[key] = item
            normalized.append(item)
            continue

        previous["score"] = max(float(previous.get("score") or 0), float(item.get("score") or 0))
        previous["clause_ids"] = list(
            dict.fromkeys(previous.get("clause_ids", []) + item.get("clause_ids", []))
        )
        if len(item.get("_context_content") or "") > len(previous.get("_context_content") or ""):
            previous["_context_content"] = item["_context_content"]
        for field in (
            "page_key", "path", "title", "page_start", "page_end", "start_char",
            "end_char", "section", "parent_index", "document_id", "source_document_id",
        ):
            if previous.get(field) in (None, "") and item.get(field) not in (None, ""):
                previous[field] = item[field]
        if item.get("strong_anchor"):
            previous.update(
                anchor_clause=item.get("anchor_clause"),
                anchor_source="explicit_query",
                strong_anchor=True,
            )
    return normalized, raw_count


def _context_excerpt(
    text: str,
    query: str,
    limit: int,
    *,
    fallback: str = "",
) -> tuple[str, int, int]:
    """Return a query-centered excerpt and its offsets in the supplied text."""
    text = text or fallback or ""
    if limit <= 0 or not text:
        return "", 0, 0
    if len(text) <= limit:
        return text, 0, len(text)

    from app.services.clause_index import extract_clause_ids

    terms = extract_clause_ids(query or "")
    terms += re.findall(r"[A-Za-z0-9_]{2,}", query or "")
    cjk_runs = re.findall(r"[\u4e00-\u9fff]{2,}", query or "")
    terms += cjk_runs
    terms += [run[i : i + 4] for run in cjk_runs for i in range(max(0, len(run) - 3))]
    terms = sorted(set(terms), key=len, reverse=True)
    lower = text.lower()
    position = next((lower.find(term.lower()) for term in terms if term.lower() in lower), -1)
    if position < 0:
        clipped = text[:limit]
        return clipped, 0, min(len(text), limit)

    inner_limit = max(1, limit - 2)
    start = max(0, position - inner_limit // 3)
    end = min(len(text), start + inner_limit)
    start = max(0, end - inner_limit)
    clipped = ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
    return clipped[:limit], start, end


def _context_header(citation: Mapping[str, Any]) -> str:
    label = str(citation.get("label") or "")
    title = str(citation.get("title") or "")
    path = str(citation.get("path") or "")
    parts = [f"[{label}] {title} ({path})"]
    if citation.get("page_key"):
        parts.append(f"page_key={citation['page_key']}")
    if citation.get("source_chunk_id") is not None:
        parts.append(f"source_chunk_id={citation['source_chunk_id']}")
    start, end = citation.get("start_char"), citation.get("end_char")
    if start is not None or end is not None:
        parts.append(f"chars={start if start is not None else '?'}-{end if end is not None else '?'}")
    page_start, page_end = citation.get("page_start"), citation.get("page_end")
    if page_start is not None or page_end is not None:
        parts.append(f"pages={page_start if page_start is not None else '?'}-{page_end if page_end is not None else '?'}")
    if citation.get("section"):
        parts.append(f"section={citation['section']}")
    if citation.get("anchor_clause"):
        parts.append(f"锚定条款={citation['anchor_clause']}")
    elif citation.get("clause_ids"):
        parts.append("含条款=" + ",".join(citation["clause_ids"][:8]))
    return " ".join(parts) + "\n"


def _fair_render_context_group(
    citations: list[dict[str, Any]],
    hits: list[dict[str, Any]],
    *,
    budget: int,
    item_cap: int,
    group_cap: int | None,
    query: str,
) -> str:
    """Render a group with equal initial shares and bounded redistribution."""
    if budget <= 0 or not citations:
        return ""
    headers = [_context_header(citation) for citation in citations]
    contents = [hit.get("_context_content") or "" for hit in hits]
    separator_cost = 2
    header_cost = sum(len(header) for header in headers) + separator_cost * max(0, len(headers) - 1)
    selected = list(range(len(citations)))
    if header_cost > budget:
        selected = []
        used = 0
        for index, header in enumerate(headers):
            cost = len(header) + (separator_cost if selected else 0)
            if selected and used + cost > budget:
                break
            selected.append(index)
            used += cost
        if not selected:
            selected = [0]

    selected_header_cost = sum(len(headers[i]) for i in selected) + separator_cost * max(0, len(selected) - 1)
    body_budget = max(0, budget - selected_header_cost)
    limits = [min(max(0, item_cap), len(contents[i])) for i in selected]
    allocations = [0 for _ in selected]
    group_used: dict[str, int] = {}
    share = body_budget // len(selected) if selected else 0
    for offset, index in enumerate(selected):
        group = str(citations[index].get("_context_group") or "")
        allowed = limits[offset]
        if group_cap is not None:
            allowed = min(allowed, max(0, group_cap - group_used.get(group, 0)))
        allocations[offset] = min(allowed, share)
        group_used[group] = group_used.get(group, 0) + allocations[offset]

    remaining = body_budget - sum(allocations)
    while remaining > 0:
        changed = False
        for offset, index in enumerate(selected):
            group = str(citations[index].get("_context_group") or "")
            allowed = limits[offset] - allocations[offset]
            if group_cap is not None:
                allowed = min(allowed, max(0, group_cap - group_used.get(group, 0)))
            if allowed <= 0:
                continue
            step = min(allowed, remaining)
            allocations[offset] += step
            group_used[group] = group_used.get(group, 0) + step
            remaining -= step
            changed = True
            if remaining <= 0:
                break
        if not changed:
            break

    blocks: list[str] = []
    included = set(selected)
    for offset, index in enumerate(selected):
        citation = citations[index]
        body, start, end = _context_excerpt(
            contents[index], query, allocations[offset], fallback=str(citation.get("snippet") or "")
        )
        citation["included"] = True
        citation["context_chars"] = len(headers[index]) + len(body)
        citation["excerpt_start_char"] = start
        citation["excerpt_end_char"] = end
        blocks.append(headers[index] + body)
    for index, citation in enumerate(citations):
        if index not in included:
            citation["included"] = False
            citation["context_chars"] = 0
    return "\n\n".join(blocks)[:budget]


def _build_context_index(citations: list[dict[str, Any]], budget: int) -> str:
    if budget <= 0 or not citations:
        return ""
    lines: list[str] = []
    per_line = max(80, budget // len(citations))
    for citation in citations:
        kind = citation.get("citation_type") or ""
        identity = (
            f"page_key={citation.get('page_key')}"
            if kind == "wiki"
            else f"source_chunk_id={citation.get('source_chunk_id')}"
        )
        anchor = f" anchor={citation['anchor_clause']}" if citation.get("anchor_clause") else ""
        lines.append(
            f"[{citation.get('label')}] {kind} {citation.get('title') or ''} | {identity}"
            f" | path={citation.get('path') or ''}{anchor}"
        )
    return "\n".join(line[:per_line] for line in lines)[:budget]


def assemble_task_context(
    wiki_hits: Iterable[Mapping[str, Any]] | None,
    source_hits: Iterable[Mapping[str, Any]] | None,
    *,
    query: str = "",
    max_chars: int | None = None,
    wiki_ratio: float = _WIKI_CONTEXT_RATIO,
    source_ratio: float = _SOURCE_CONTEXT_RATIO,
    index_ratio: float = _INDEX_CONTEXT_RATIO,
    max_wiki_item_chars: int = _DEFAULT_WIKI_ITEM_CHARS,
    max_source_item_chars: int = _DEFAULT_SOURCE_ITEM_CHARS,
    max_source_document_chars: int | None = None,
    include_explain: bool = False,
) -> dict[str, Any]:
    """Assemble fair Wiki/source context plus traceable citations.

    The return value is intentionally a plain mapping so callers can pass
    ``wiki_context`` and ``source_context`` into the existing generation
    message builder without changing its API.  Strong clause anchors are
    derived only from clause ids explicitly present in ``query``.
    """
    ratios = [float(wiki_ratio), float(source_ratio), float(index_ratio)]
    if any(r < 0 for r in ratios) or sum(ratios) <= 0:
        raise ValueError("context ratios must be non-negative and not all zero")
    total = int(max_chars) if max_chars is not None else max(
        int(round(config.MAX_WIKI_CONTEXT_CHARS / _WIKI_CONTEXT_RATIO)),
        int(round(config.MAX_SOURCE_CONTEXT_CHARS / _SOURCE_CONTEXT_RATIO)),
        1,
    )
    total = max(0, total)
    ratio_sum = sum(ratios)
    budgets = [int(total * ratio / ratio_sum) for ratio in ratios]
    for index in range(total - sum(budgets)):
        budgets[index % len(budgets)] += 1

    explicit_clause_ids = _context_explicit_clauses(query)
    raw_wiki = list(wiki_hits or [])
    raw_source = list(source_hits or [])
    wiki, raw_wiki_count = _normalise_context_hits(
        raw_wiki, kind="wiki", explicit_clause_ids=explicit_clause_ids
    )
    source, raw_source_count = _normalise_context_hits(
        raw_source, kind="source", explicit_clause_ids=explicit_clause_ids
    )

    citations: list[dict[str, Any]] = []
    for index, hit in enumerate(wiki, start=1):
        text = hit.get("_context_content") or ""
        snippet, _, _ = _context_excerpt(text, query, 320, fallback=str(hit.get("snippet") or ""))
        citations.append(
            {
                "citation_type": "wiki",
                "citation_id": f"W{index}",
                "label": str(index),
                "wiki_page_id": hit.get("id"),
                "page_key": hit.get("page_key"),
                "source_chunk_id": None,
                "title": hit.get("title") or "",
                "path": hit.get("path") or "",
                "score": float(hit.get("score") or 0),
                "snippet": snippet,
                "content_excerpt": text[:2000],
                "start_char": hit.get("start_char"),
                "end_char": hit.get("end_char"),
                "page_start": hit.get("page_start"),
                "page_end": hit.get("page_end"),
                "section": hit.get("section") or "",
                "clause_ids": hit.get("clause_ids") or [],
                "anchor_clause": None,
                "document_id": hit.get("source_document_id") or hit.get("document_id"),
                "_context_group": hit.get("_context_group"),
            }
        )
    wiki_citations = citations[:]
    for index, hit in enumerate(source, start=1):
        text = hit.get("_context_content") or ""
        snippet, _, _ = _context_excerpt(text, query, 320, fallback=str(hit.get("snippet") or ""))
        citations.append(
            {
                "citation_type": "source",
                "citation_id": f"S{index}",
                "label": f"S{index}",
                "wiki_page_id": None,
                "page_key": hit.get("page_key"),
                "source_chunk_id": hit.get("source_chunk_id") or hit.get("id"),
                "title": hit.get("title") or f"原文块{index}",
                "path": hit.get("path") or "",
                "score": float(hit.get("score") or 0),
                "snippet": snippet,
                "content_excerpt": text[:2000],
                "start_char": hit.get("start_char"),
                "end_char": hit.get("end_char"),
                "page_start": hit.get("page_start"),
                "page_end": hit.get("page_end"),
                "section": hit.get("section") or "",
                "clause_ids": hit.get("clause_ids") or [],
                "anchor_clause": hit.get("anchor_clause"),
                "strong_anchor": bool(hit.get("strong_anchor")),
                "document_id": hit.get("document_id") or hit.get("source_document_id"),
                "_context_group": hit.get("_context_group"),
            }
        )
    source_citations = citations[len(wiki_citations) :]

    wiki_context = _fair_render_context_group(
        wiki_citations,
        wiki,
        budget=budgets[0],
        item_cap=max(0, int(max_wiki_item_chars)),
        group_cap=max(0, int(max_wiki_item_chars)),
        query=query,
    )
    source_group_cap = max_source_document_chars
    if source_group_cap is None:
        source_group_cap = max(0, min(budgets[1], max(int(max_source_item_chars), budgets[1] // 2)))
    source_context = _fair_render_context_group(
        source_citations,
        source,
        budget=budgets[1],
        item_cap=max(0, int(max_source_item_chars)),
        group_cap=source_group_cap,
        query=query,
    )
    index_context = _build_context_index(citations, budgets[2])
    result: dict[str, Any] = {
        "text": (
            "# Wiki 结构化知识\n" + (wiki_context or "（无匹配 Wiki 页面）") + "\n\n"
            "# 原文证据\n" + (source_context or "（无匹配原文块）") + "\n\n"
            "# 关联索引\n" + (index_context or "（无命中索引）")
        ),
        "wiki_context": wiki_context,
        "source_context": source_context,
        "index_context": index_context,
        "citations": citations,
        "wiki_hits": [{k: v for k, v in hit.items() if not k.startswith("_")} for hit in wiki],
        "source_hits": [{k: v for k, v in hit.items() if not k.startswith("_")} for hit in source],
        "explicit_clause_ids": explicit_clause_ids,
        "explicit_anchor_clause_ids": list(
            dict.fromkeys(
                citation["anchor_clause"]
                for citation in source_citations
                if citation.get("strong_anchor") and citation.get("anchor_clause")
            )
        ),
        "budgets": {
            "total_chars": total,
            "wiki_chars": budgets[0],
            "source_chars": budgets[1],
            "index_chars": budgets[2],
            "ratios": {
                "wiki": ratios[0] / ratio_sum,
                "source": ratios[1] / ratio_sum,
                "index": ratios[2] / ratio_sum,
            },
        },
    }
    if include_explain:
        result["explain"] = {
            "deduped": {
                "wiki": raw_wiki_count - len(wiki),
                "source": raw_source_count - len(source),
            },
            "counts": {
                "wiki": len(wiki),
                "source": len(source),
                "included": sum(1 for citation in citations if citation.get("included")),
            },
            "explicit_clause_ids": explicit_clause_ids,
            "explicit_anchor_clause_ids": result["explicit_anchor_clause_ids"],
            "item_caps": {
                "wiki": int(max_wiki_item_chars),
                "source": int(max_source_item_chars),
                "source_document": int(source_group_cap),
            },
        }
    return result


# Descriptive alias for callers that prefer the verb “build”.
build_task_context = assemble_task_context


def _build_messages(
    system_prompt: str,
    requirement: Requirement,
    wiki_context: str,
    source_context: str = "",
    index_context: str = "",
) -> list[dict[str, str]]:
    tags = _focus_tags(requirement)
    user_parts = [
        "# 需求 [REQ]",
        f"标题：{requirement.title}",
        f"描述：{requirement.description}",
    ]
    if tags:
        user_parts.append(f"关注标签：{', '.join(tags)}")
    user_parts.append("")
    user_parts.append("# Wiki 结构化知识（摘要/规则卡片）")
    user_parts.append(wiki_context if wiki_context.strip() else "（无匹配 Wiki 页面）")
    user_parts.append("")
    user_parts.append("# 原文摘录（Source Chunks，请优先引用可核对的原句）")
    user_parts.append(
        source_context if source_context.strip() else "（无匹配原文块）"
    )
    user_parts.append("")
    user_parts.append("# 关联索引（命中元数据）")
    user_parts.append(index_context if index_context.strip() else "（无命中索引）")
    user_parts.append("")
    user_parts.append(
        "请根据需求、Wiki 与【原文摘录】生成测试用例 Markdown。\n"
        "硬性要求：\n"
        "1) 规则断言必须能在原文 [S#] 中找到依据，优先引用条款号（如 3.5.2）；\n"
        "2) 每条用例「关联知识」同时写 Wiki 编号 [n] 与原文 [S#]（若有）；\n"
        "3) 需要定位时保留 page_key、source_chunk_id、字符范围、页码和条款号；\n"
        "4) 覆盖正常 / 边界 / 异常；不得编造上下文未出现的规则；\n"
        "5) 只输出用例 Markdown。"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def _resolve_chat_fn(
    chat_fn: Optional[ChatFn],
    stage_fn: Optional[ChatFn] = None,
) -> Optional[ChatFn]:
    if chat_fn is not None:
        return chat_fn
    if stage_fn is not None:
        return stage_fn
    if _PIPELINE_CHAT_FN is not None:
        return _PIPELINE_CHAT_FN
    return _GENERATE_CHAT_FN


def _call_chat(
    chat_fn: Optional[ChatFn],
    *,
    model: ModelConfig,
    messages: list[dict[str, str]],
    stage_fn: Optional[ChatFn] = None,
    stream: bool = False,
    on_attempt: Optional[Callable[[int, bool], None]] = None,
    on_delta: Optional[Callable[[str], None]] = None,
    on_retry: Optional[Callable[[int, str], None]] = None,
) -> str:
    fn = _resolve_chat_fn(chat_fn, stage_fn)
    if fn is not None:
        if on_attempt is not None:
            on_attempt(1, False)
        try:
            result = fn(messages=messages, model=model)
        except TypeError:
            # Allow simpler signatures used in tests: fn(messages) or fn(**kwargs)
            try:
                result = fn(messages)
            except TypeError:
                result = fn(
                    base_url=model.base_url,
                    api_key=model.api_key,
                    model=model.model_name,
                    messages=messages,
                )
        content = str(result[0]) if isinstance(result, tuple) else str(result)
        if stream and on_delta is not None and content:
            # Test/injected hooks are non-streaming by design.  Publishing the
            # completed hook result as one fragment still gives the UI a live
            # preview without changing the existing hook contract.
            on_delta(content)
        return content

    content, _usage = chat_completion(
        base_url=model.base_url,
        api_key=model.api_key,
        model=model.model_name,
        messages=messages,
        stream=stream,
        on_attempt=on_attempt,
        on_delta=on_delta,
        on_retry=on_retry,
    )
    return content


def _latest_draft(session: Session, task_id: int) -> Optional[CaseDraft]:
    return session.exec(
        select(CaseDraft)
        .where(CaseDraft.task_id == task_id)
        .order_by(col(CaseDraft.version).desc())
    ).first()


def _latest_review(session: Session, task_id: int) -> Optional[ReviewResult]:
    return session.exec(
        select(ReviewResult)
        .where(ReviewResult.task_id == task_id)
        .order_by(col(ReviewResult.id).desc())
    ).first()


def _resolve_prompt_by_type(session: Session, prompt_type: str) -> PromptTemplate:
    active = session.exec(
        select(PromptTemplate).where(
            PromptTemplate.type == prompt_type,
            PromptTemplate.is_active == True,  # noqa: E712
        ).order_by(col(PromptTemplate.updated_at).desc(), col(PromptTemplate.id).desc())
    ).first()
    if active is None:
        raise RuntimeError(f"No active {prompt_type} prompt template found")
    return active


def _resolve_review_model(session: Session, task: GenerationTask) -> ModelConfig:
    if task.review_model_id is not None:
        row = session.get(ModelConfig, task.review_model_id)
        if row is None:
            raise RuntimeError(f"ModelConfig id={task.review_model_id} not found")
        return row
    return _resolve_model(session, task)


def _citations_for_task(session: Session, task_id: int) -> list[TaskCitation]:
    return list(
        session.exec(
            select(TaskCitation)
            .where(TaskCitation.task_id == task_id)
            .order_by(col(TaskCitation.id).asc())
        ).all()
    )


def _build_review_messages(
    system_prompt: str,
    requirement: Requirement,
    draft: CaseDraft,
    citations: list[TaskCitation],
) -> list[dict[str, str]]:
    tags = _focus_tags(requirement)
    cite_lines: list[str] = []
    wiki_index = 0
    source_index = 0
    for citation in citations:
        if citation.citation_type == "source":
            source_index += 1
            label = f"[S{source_index}]"
            try:
                clauses = json.loads(citation.clause_ids_json or "[]")
            except json.JSONDecodeError:
                clauses = []
            metadata = [f"source_chunk_id={citation.source_chunk_id}"]
            if citation.anchor_clause:
                metadata.append(f"锚定条款={citation.anchor_clause}")
            elif isinstance(clauses, list) and clauses:
                metadata.append(f"含条款={','.join(str(item) for item in clauses[:8])}")
            excerpt = citation.content_excerpt or citation.snippet or ""
        else:
            wiki_index += 1
            label = f"[{wiki_index}]"
            metadata = [f"wiki_page_id={citation.wiki_page_id}"]
            excerpt = citation.snippet or citation.content_excerpt or ""
        cite_lines.append(
            f"{label} {citation.title} ({citation.path}) "
            f"score={citation.score} {' '.join(metadata)}\n{excerpt}"
        )
    user_parts = [
        "# 需求",
        f"标题：{requirement.title}",
        f"描述：{requirement.description}",
    ]
    if tags:
        user_parts.append(f"关注标签：{', '.join(tags)}")
    user_parts.append("")
    user_parts.append("# 引用证据（Wiki [n] / 原文 [S#]，标签与生成阶段一致）")
    user_parts.append("\n\n".join(cite_lines) if cite_lines else "（无引用）")
    user_parts.append("")
    user_parts.append(f"# 用例草稿 v{draft.version}")
    user_parts.append(draft.content_md or "")
    user_parts.append("")
    user_parts.append(f"终版分数门槛：{config.FINAL_SCORE_THRESHOLD}")
    user_parts.append("请按系统要求输出评审 JSON。")
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def _build_optimize_messages(
    system_prompt: str,
    current_generate_prompt: str,
    requirement: Requirement,
    review_payload: dict[str, Any],
) -> list[dict[str, str]]:
    tags = _focus_tags(requirement)
    user_parts = [
        "# 当前 generate 提示词",
        current_generate_prompt,
        "",
        "# 需求摘要",
        f"标题：{requirement.title}",
        f"描述：{requirement.description}",
    ]
    if tags:
        user_parts.append(f"关注标签：{', '.join(tags)}")
    user_parts.append("")
    user_parts.append("# 评审结果")
    user_parts.append(json.dumps(review_payload, ensure_ascii=False, indent=2))
    user_parts.append("")
    user_parts.append("请输出优化后的完整 generate 提示词正文。")
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def _next_prompt_version(session: Session, prompt_type: str) -> int:
    current = session.exec(
        select(func.max(PromptTemplate.version)).where(PromptTemplate.type == prompt_type)
    ).one()
    if current is None:
        return 1
    return int(current) + 1


def _next_draft_version(session: Session, task_id: int) -> int:
    current = session.exec(
        select(func.max(CaseDraft.version)).where(CaseDraft.task_id == task_id)
    ).one()
    if current is None:
        return 1
    return int(current) + 1


def _fail_task(
    session: Session,
    task: GenerationTask,
    message: str,
    *,
    publish_stream: bool = False,
) -> GenerationTask:
    try:
        if task.status != "failed":
            _set_status(task, "failed")
    except InvalidTransition:
        task.status = "failed"
        task.updated_at = _utcnow()
    task.error_message = message
    session.add(task)
    append_event(session, task.id, "error", message)
    session.commit()
    session.refresh(task)
    if publish_stream:
        # This is intentionally post-commit so the SSE terminal event agrees
        # with the durable task state. Review/optimize failures use the same
        # helper but must not overwrite a retained generation terminal state.
        task_stream.fail(task.id, message=message)
    return task


def run_generate(
    session: Session,
    task_id: int,
    chat_fn: Optional[ChatFn] = None,
) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise ValueError(f"GenerationTask id={task_id} not found")
    stream_task_id = int(task.id or task_id)
    existing_stream = task_stream.snapshot(stream_task_id)
    if existing_stream is None or existing_stream.get("terminal") is not None:
        task_stream.start(
            stream_task_id,
            status=task.status,
            message="后台生成任务已启动",
        )

    requirement = session.get(Requirement, task.requirement_id)
    if requirement is None:
        return _fail_task(
            session,
            task,
            f"Requirement id={task.requirement_id} not found",
            publish_stream=True,
        )

    try:
        # Always re-retrieve: draft/failed/regenerating → retrieving
        if task.status in ("draft", "failed", "regenerating"):
            _set_status(task, "retrieving")
            session.add(task)
            append_event(session, task.id, "retrieve", "开始混合检索（Wiki + 原文块）")
            session.commit()
            session.refresh(task)
        elif task.status != "retrieving":
            raise InvalidTransition(
                f"Cannot start generate from status {task.status!r}"
            )

        task_stream.status(
            stream_task_id,
            status="retrieving",
            message="正在检索 Wiki 与原文证据",
        )

        query = _build_query(requirement)
        resolved_space_id = resolve_space_id(session, task.wiki_space_id)
        if task.wiki_space_id is None:
            task.wiki_space_id = resolved_space_id
            session.add(task)
            session.commit()
        from app.services.hybrid_retrieve import hybrid_retrieve

        retrieved = hybrid_retrieve(
            session,
            query,
            wiki_k=config.RETRIEVE_WIKI_TOP_K,
            source_k=config.RETRIEVE_SOURCE_TOP_K,
            top_k=config.RETRIEVE_WIKI_TOP_K + config.RETRIEVE_SOURCE_TOP_K,
            space_id=resolved_space_id,
        )
        context = assemble_task_context(
            retrieved.get("wiki_hits") or [],
            retrieved.get("source_hits") or [],
            query=query,
            include_explain=True,
        )
        wiki_hits = list(context["wiki_hits"])
        source_hits = list(context["source_hits"])

        _clear_citations(session, task.id)
        for citation in context["citations"]:
            cids = list(citation.get("clause_ids") or [])
            session.add(
                TaskCitation(
                    task_id=task.id,
                    citation_type=citation.get("citation_type") or "wiki",
                    wiki_page_id=citation.get("wiki_page_id"),
                    source_chunk_id=citation.get("source_chunk_id"),
                    title=citation.get("title") or "",
                    path=citation.get("path") or "",
                    score=float(citation.get("score") or 0.0),
                    snippet=citation.get("snippet") or "",
                    content_excerpt=citation.get("content_excerpt") or "",
                    clause_ids_json=json.dumps(cids, ensure_ascii=False),
                    # Only explicit query clauses survive context assembly.
                    anchor_clause=citation.get("anchor_clause"),
                )
            )
        hit_count = len(context["citations"])
        explicit_anchors = context["explicit_anchor_clause_ids"]
        append_event(
            session,
            task.id,
            "retrieve",
            f"检索完成：Wiki {len(wiki_hits)} + 原文 {len(source_hits)}"
            + (
                f"（用户显式条款锚定 {len(explicit_anchors)}）"
                if explicit_anchors
                else ""
            ),
            detail={
                "query": query,
                "wiki_hit_count": len(wiki_hits),
                "source_hit_count": len(source_hits),
                "hit_count": hit_count,
                "clause_ids": context["explicit_clause_ids"],
                "anchored_clause_ids": explicit_anchors,
                "context_budgets": context["budgets"],
                "context_explain": context.get("explain"),
            },
        )
        if hit_count == 0:
            append_event(
                session,
                task.id,
                "retrieve",
                "警告：未检索到 Wiki 或原文块，将仅基于需求生成",
            )
        session.commit()
        session.refresh(task)

        _set_status(task, "generating")
        session.add(task)
        append_event(session, task.id, "generate", "开始调用 LLM 生成用例")
        session.commit()
        session.refresh(task)
        task_stream.status(
            stream_task_id,
            status="generating",
            message="正在生成测试用例",
        )

        system_prompt, prompt_ref = _resolve_generate_prompt(session, task)
        model = _resolve_model(session, task)
        wiki_context = context["wiki_context"]
        source_context = context["source_context"]
        index_context = context["index_context"]
        messages = _build_messages(
            system_prompt, requirement, wiki_context, source_context, index_context
        )

        def publish_attempt(attempt: int, reset: bool) -> None:
            if reset:
                # Upstream attempts are independent.  The old partial text is
                # deliberately discarded before the replacement attempt emits
                # its first delta.
                task_stream.reset(
                    stream_task_id,
                    status="generating",
                    message=f"上游第 {attempt} 次尝试，已替换此前未完成输出",
                )
            else:
                task_stream.notice(
                    stream_task_id,
                    message=f"开始接收模型输出（第 {attempt} 次尝试）",
                )

        def publish_delta(delta: str) -> None:
            task_stream.delta(stream_task_id, delta)

        def publish_retry(next_attempt: int, message: str) -> None:
            task_stream.retry(
                stream_task_id,
                attempt=next_attempt,
                message=f"模型连接重试（第 {next_attempt} 次）：{message[:300]}",
            )

        used_lean_fallback = False
        try:
            content = _call_chat(
                chat_fn,
                model=model,
                messages=messages,
                stage_fn=_GENERATE_CHAT_FN,
                stream=True,
                on_attempt=publish_attempt,
                on_delta=publish_delta,
                on_retry=publish_retry,
            )
        except LLMError as primary_exc:
            # Gateway instability on long finance prompts: retry with the same
            # fair allocation contract and a smaller total budget.
            lean_cap = min(4500, max(2000, config.MAX_WIKI_CONTEXT_CHARS // 2))
            lean_total = min(
                context["budgets"]["total_chars"],
                lean_cap + min(2500, config.MAX_SOURCE_CONTEXT_CHARS // 2 or 2500) + 500,
            )
            lean_context = assemble_task_context(
                wiki_hits,
                source_hits,
                query=query,
                max_chars=lean_total,
                include_explain=True,
            )
            lean_wiki = lean_context["wiki_context"]
            lean_source = lean_context["source_context"]
            lean_system_prompt = (
                system_prompt.rstrip()
                + "\n\n# 精简重试补充\n"
                + _LEAN_GENERATE_SYSTEM
            )
            lean_messages = _build_messages(
                lean_system_prompt,
                requirement,
                lean_wiki,
                lean_source,
                lean_context["index_context"],
            )
            append_event(
                session,
                task.id,
                "generate",
                f"主生成失败，精简上下文重试: {primary_exc}",
                detail={
                    "lean_wiki_chars": len(lean_wiki),
                    "lean_source_chars": len(lean_source),
                    "lean_index_chars": len(lean_context["index_context"]),
                    "lean_budgets": lean_context["budgets"],
                    "primary_error": str(primary_exc)[:300],
                },
            )
            session.commit()
            task_stream.reset(
                stream_task_id,
                status="generating",
                message="主生成失败，已清空未完成输出并切换精简上下文",
            )
            task_stream.notice(
                stream_task_id,
                message="正在使用精简上下文重新生成",
            )
            content = _call_chat(
                chat_fn,
                model=model,
                messages=lean_messages,
                stage_fn=_GENERATE_CHAT_FN,
                stream=True,
                on_attempt=publish_attempt,
                on_delta=publish_delta,
                on_retry=publish_retry,
            )
            used_lean_fallback = True
            prompt_ref = f"{prompt_ref}|lean_fallback"

        if not content or not str(content).strip():
            raise LLMError("Empty LLM content")

        version = _next_draft_version(session, task.id)
        draft = CaseDraft(
            task_id=task.id,
            version=version,
            content_md=str(content),
            prompt_version_ref=prompt_ref,
        )
        session.add(draft)

        _set_status(task, "generated")
        task.error_message = None
        session.add(task)
        append_event(
            session,
            task.id,
            "generate",
            f"生成完成，draft v{version}"
            + ("（lean_fallback）" if used_lean_fallback else ""),
            detail={
                "draft_version": version,
                "model_id": model.id,
                "prompt_ref": prompt_ref,
                "lean_fallback": used_lean_fallback,
            },
        )
        session.commit()
        session.refresh(task)
        task_stream.complete(
            stream_task_id,
            text=str(content),
            status="generated",
            message=f"生成完成，draft v{version}",
        )
        return task

    except InvalidTransition as exc:
        return _fail_task(session, task, str(exc), publish_stream=True)
    except LLMError as exc:
        return _fail_task(
            session,
            task,
            f"LLM error: {exc}",
            publish_stream=True,
        )
    except Exception as exc:  # noqa: BLE001 — surface any pipeline failure on the task
        return _fail_task(session, task, str(exc), publish_stream=True)


def run_review(
    session: Session,
    task_id: int,
    chat_fn: Optional[ChatFn] = None,
) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise ValueError(f"GenerationTask id={task_id} not found")

    draft = _latest_draft(session, task.id)
    if draft is None:
        return _fail_task(session, task, "No case draft available for review")

    requirement = session.get(Requirement, task.requirement_id)
    if requirement is None:
        return _fail_task(session, task, f"Requirement id={task.requirement_id} not found")

    try:
        if task.status in ("generated", "failed"):
            _set_status(task, "reviewing")
            session.add(task)
            append_event(session, task.id, "review", "开始评审用例")
            session.commit()
            session.refresh(task)
        elif task.status != "reviewing":
            raise InvalidTransition(f"Cannot start review from status {task.status!r}")

        review_prompt = _resolve_prompt_by_type(session, "review")
        model = _resolve_review_model(session, task)
        citations = _citations_for_task(session, task.id)
        messages = _build_review_messages(
            review_prompt.content, requirement, draft, citations
        )

        content = _call_chat(
            chat_fn,
            model=model,
            messages=messages,
            stage_fn=_REVIEW_CHAT_FN,
        )
        payload = parse_review_payload(str(content) if content is not None else "")
        score = int(payload.get("score") or 0)
        verdict = str(payload.get("verdict") or "unknown")

        result = ReviewResult(
            task_id=task.id,
            draft_id=draft.id,
            score=score,
            verdict=verdict,
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        session.add(result)

        _set_status(task, "reviewed")
        task.error_message = None
        session.add(task)
        append_event(
            session,
            task.id,
            "review",
            f"评审完成 score={score} verdict={verdict}",
            detail={
                "score": score,
                "verdict": verdict,
                "draft_id": draft.id,
                "ready_for_final": payload.get("ready_for_final"),
            },
        )
        session.commit()
        session.refresh(task)
        return task

    except InvalidTransition as exc:
        return _fail_task(session, task, str(exc))
    except LLMError as exc:
        return _fail_task(session, task, f"LLM error: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _fail_task(session, task, str(exc))


def run_optimize_prompt(
    session: Session,
    task_id: int,
    chat_fn: Optional[ChatFn] = None,
) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise ValueError(f"GenerationTask id={task_id} not found")

    requirement = session.get(Requirement, task.requirement_id)
    if requirement is None:
        return _fail_task(session, task, f"Requirement id={task.requirement_id} not found")

    review = _latest_review(session, task.id)
    if review is None:
        return _fail_task(session, task, "No review result available for optimize")

    try:
        review_payload = json.loads(review.payload_json or "{}")
    except json.JSONDecodeError:
        review_payload = parse_review_payload(review.payload_json or "")

    try:
        if task.status in ("reviewed", "failed"):
            _set_status(task, "optimizing")
            session.add(task)
            append_event(session, task.id, "optimize", "开始优化 generate 提示词")
            session.commit()
            session.refresh(task)
        elif task.status != "optimizing":
            raise InvalidTransition(
                f"Cannot start optimize from status {task.status!r}"
            )

        generate_content, _ref = _resolve_generate_prompt(session, task)
        base_prompt_id: Optional[int] = None
        if task.prompt_template_id is not None:
            base_prompt_id = task.prompt_template_id
        else:
            active_gen = session.exec(
                select(PromptTemplate).where(
                    PromptTemplate.type == "generate",
                    PromptTemplate.is_active == True,  # noqa: E712
                ).order_by(
                    col(PromptTemplate.updated_at).desc(),
                    col(PromptTemplate.id).desc(),
                )
            ).first()
            if active_gen is not None:
                base_prompt_id = active_gen.id

        optimize_prompt = _resolve_prompt_by_type(session, "optimize")
        model = _resolve_model(session, task)
        messages = _build_optimize_messages(
            optimize_prompt.content,
            generate_content,
            requirement,
            review_payload if isinstance(review_payload, dict) else {},
        )

        content = _call_chat(
            chat_fn,
            model=model,
            messages=messages,
            stage_fn=_OPTIMIZE_CHAT_FN,
        )
        new_content = str(content or "").strip()
        if not new_content:
            raise LLMError("Empty optimized prompt content")

        revision = PromptRevision(
            task_id=task.id,
            base_prompt_id=base_prompt_id,
            new_content=new_content,
            status="pending",
        )
        session.add(revision)

        _set_status(task, "reviewed")
        task.error_message = None
        session.add(task)
        append_event(
            session,
            task.id,
            "optimize",
            "提示词优化完成，revision pending",
            detail={"base_prompt_id": base_prompt_id},
        )
        session.commit()
        session.refresh(task)
        return task

    except InvalidTransition as exc:
        return _fail_task(session, task, str(exc))
    except LLMError as exc:
        return _fail_task(session, task, f"LLM error: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _fail_task(session, task, str(exc))


def apply_prompt(
    session: Session,
    task_id: int,
    revision_id: int,
    mode: str,
    content: str | None = None,
) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise ValueError(f"GenerationTask id={task_id} not found")

    revision = session.get(PromptRevision, revision_id)
    if revision is None or revision.task_id != task.id:
        raise ValueError(f"PromptRevision id={revision_id} not found for task")
    if revision.status != "pending":
        raise ValueError("Only pending prompt revisions can be applied")

    if mode not in ("global", "task_temp"):
        raise ValueError(f"Invalid apply mode {mode!r}")

    edited = content is not None
    if content is not None:
        normalized_content = content.strip()
        if not normalized_content:
            raise ValueError("Prompt content must not be empty")
        revision.new_content = normalized_content
        session.add(revision)

    if mode == "task_temp":
        task.temp_prompt_content = revision.new_content
        session.add(task)
        revision.status = "applied_task_temp"
        session.add(revision)
        append_event(
            session,
            task.id,
            "apply_prompt",
            f"已应用 revision#{revision_id} 为任务临时提示词",
            detail={"mode": mode, "revision_id": revision_id, "edited": edited},
        )
    else:
        # global: new active generate PromptTemplate version
        base_name = "generate"
        if revision.base_prompt_id is not None:
            base = session.get(PromptTemplate, revision.base_prompt_id)
            if base is not None:
                base_name = base.name
        version = _next_prompt_version(session, "generate")
        row = PromptTemplate(
            name=base_name,
            type="generate",
            content=revision.new_content,
            version=version,
            is_active=True,
        )
        session.add(row)
        session.flush()
        task.prompt_template_id = row.id
        task.temp_prompt_content = None
        session.add(task)
        revision.status = "applied_global"
        session.add(revision)
        append_event(
            session,
            task.id,
            "apply_prompt",
            f"已应用 revision#{revision_id} 为全局 generate v{version}",
            detail={
                "mode": mode,
                "revision_id": revision_id,
                "prompt_template_id": row.id,
                "version": version,
                "edited": edited,
            },
        )

    task.updated_at = _utcnow()
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def run_regenerate(
    session: Session,
    task_id: int,
    chat_fn: Optional[ChatFn] = None,
) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise ValueError(f"GenerationTask id={task_id} not found")

    try:
        if task.status in ("generated", "reviewed", "failed"):
            _set_status(task, "regenerating")
            session.add(task)
            append_event(session, task.id, "regenerate", "开始重新生成")
            session.commit()
            session.refresh(task)
        elif task.status != "regenerating":
            raise InvalidTransition(
                f"Cannot regenerate from status {task.status!r}"
            )
    except InvalidTransition as exc:
        return _fail_task(session, task, str(exc), publish_stream=True)

    return run_generate(session, task_id, chat_fn=chat_fn)


def finalize_task(
    session: Session,
    task_id: int,
    draft_id: int | None = None,
) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise ValueError(f"GenerationTask id={task_id} not found")

    draft = (
        session.get(CaseDraft, draft_id)
        if draft_id is not None
        else _latest_draft(session, task.id)
    )
    if draft is None:
        raise ValueError("Cannot finalize task without a case draft")
    if draft.task_id != task.id:
        raise ValueError("Selected draft does not belong to task")

    # Repeating the exact finalize request is intentionally idempotent.  It
    # returns the same current state without adding another audit event or
    # touching manually edited cases.  Rows finalized by an older release do
    # not have finalized_draft_id; selecting their latest draft once backfills
    # the new metadata and imports that exact draft.
    if task.status == "finalized":
        if task.finalized_draft_id not in (None, draft.id):
            raise ValueError("Task is already finalized with another draft")
        if task.finalized_draft_id == draft.id:
            return task
        imported = import_cases_from_draft(session, task, draft)
        task.finalized_draft_id = draft.id
        task.finalized_at = task.finalized_at or utcnow()
        session.add(task)
        session.commit()
        session.refresh(task)
        return task

    if task.status not in {"generated", "reviewed"}:
        raise ValueError(f"Cannot finalize task from status {task.status!r}")

    # Parse and import before moving the task state.  A malformed draft must
    # leave the task retryable and must never commit a partial case set.
    imported = import_cases_from_draft(session, task, draft)

    try:
        _set_status(task, "finalized")
    except InvalidTransition as exc:
        raise ValueError(str(exc)) from exc

    task.error_message = None
    task.finalized_draft_id = draft.id
    task.finalized_at = utcnow()
    session.add(task)
    append_event(
        session,
        task.id,
        "finalize",
        f"任务已终版，draft v{draft.version}",
        detail={
            "draft_id": draft.id,
            "draft_version": draft.version,
            "imported_case_ids": [int(row.id) for row in imported if row.id is not None],
            "imported_case_count": len(imported),
        },
    )
    session.commit()
    session.refresh(task)
    return task
