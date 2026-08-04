"""Split source text while retaining PDF/section/clause anchors."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from app.services.parse_document import ParsedDocument, TextSpan


DEFAULT_CHUNK_CHARS = 1200
DEFAULT_OVERLAP_CHARS = 150
MIN_CHUNK_CHARS = 200
_CLAUSE_LINE_RE = re.compile(
    r"(?m)^[ \t]*(?P<id>"
    r"[1-9]\d*(?:\.\d+){1,3}|"
    r"第[0-9一二三四五六七八九十百千万]+条"
    r")(?=[ \t\u3000、.．:：-])"
)


def _field(span: Any, name: str, default: Any = None) -> Any:
    if isinstance(span, dict):
        return span.get(name, default)
    return getattr(span, name, default)


def _as_span(span: Any) -> dict[str, Any] | None:
    try:
        start = int(_field(span, "start_char", _field(span, "start", 0)))
        end = int(_field(span, "end_char", _field(span, "end", 0)))
    except (TypeError, ValueError):
        return None
    if end <= start:
        return None
    clause_ids = _field(span, "clause_ids", _field(span, "clauses", ())) or ()
    if isinstance(clause_ids, str):
        try:
            clause_ids = json.loads(clause_ids)
        except json.JSONDecodeError:
            clause_ids = [clause_ids]
    return {
        "start_char": start,
        "end_char": end,
        "page_start": _field(span, "page_start"),
        "page_end": _field(span, "page_end"),
        "section": _field(span, "section") or "",
        "clause_ids": [str(value) for value in clause_ids if str(value).strip()],
    }


def _normalise_spans(value: Iterable[Any] | None) -> list[dict[str, Any]]:
    return [item for raw in (value or []) if (item := _as_span(raw)) is not None]


def _overlap(span: dict[str, Any], start: int, end: int) -> int:
    return max(0, min(end, span["end_char"]) - max(start, span["start_char"]))


def _metadata_for_range(
    start: int,
    end: int,
    *,
    page_spans: list[dict[str, Any]],
    section_spans: list[dict[str, Any]],
    clause_spans: list[dict[str, Any]],
) -> dict[str, Any]:
    pages = [span for span in page_spans if _overlap(span, start, end)]
    page_starts = [span["page_start"] for span in pages if span.get("page_start") is not None]
    page_ends = [span["page_end"] for span in pages if span.get("page_end") is not None]
    sections = [
        span for span in section_spans if _overlap(span, start, end)
    ]
    sections.sort(key=lambda span: _overlap(span, start, end), reverse=True)
    section = sections[0].get("section", "") if sections else ""
    clause_ranges = [span for span in clause_spans if _overlap(span, start, end)]
    clauses: list[str] = []
    seen: set[str] = set()
    # Parsed clause ranges are precise. Page/section clause lists summarize a
    # much wider range and are only a compatibility fallback when no precise
    # clause span was supplied.
    clause_sources = clause_ranges if clause_ranges else [*pages, *sections]
    for span in clause_sources:
        for value in span.get("clause_ids", []):
            if value not in seen:
                seen.add(value)
                clauses.append(value)
    return {
        "page_start": min(page_starts) if page_starts else None,
        "page_end": max(page_ends) if page_ends else None,
        "section": section,
        "clause_ids": clauses,
        "clause_ids_json": json.dumps(clauses, ensure_ascii=False),
    }


def _clause_anchor_spans(text: str) -> list[dict[str, Any]]:
    matches = list(_CLAUSE_LINE_RE.finditer(text))
    return [
        {
            "start_char": match.start(),
            "end_char": matches[index + 1].start() if index + 1 < len(matches) else len(text),
            "page_start": None,
            "page_end": None,
            "section": "",
            "clause_ids": [match.group("id")],
        }
        for index, match in enumerate(matches)
    ]


def _document_segments(
    text: str,
    section_spans: list[dict[str, Any]],
) -> list[tuple[int, int, str, int]]:
    """Use structural sections as parents while retaining any front matter."""
    if not text:
        return []
    sections = sorted(
        (
            span
            for span in section_spans
            if 0 <= span["start_char"] < span["end_char"] <= len(text)
            and span.get("section")
        ),
        key=lambda span: (span["start_char"], span["end_char"]),
    )
    if not sections:
        return [(0, len(text), _label_for(text), 0)]

    segments: list[tuple[int, int, str, int]] = []
    first_start = sections[0]["start_char"]
    if first_start > 0 and text[:first_start].strip():
        segments.append((0, first_start, _label_for(text[:first_start]), 0))
    pending_heading_start: int | None = None
    for span in sections:
        span_text = text[span["start_char"]:span["end_char"]]
        label = str(span.get("section") or _label_for(span_text))
        if span_text.strip() == label.strip():
            if pending_heading_start is None:
                pending_heading_start = span["start_char"]
            continue
        start = pending_heading_start if pending_heading_start is not None else span["start_char"]
        segments.append(
            (
                start,
                span["end_char"],
                label,
                len(segments),
            )
        )
        pending_heading_start = None
    if pending_heading_start is not None:
        label = str(sections[-1].get("section") or _label_for(text[pending_heading_start:]))
        segments.append((pending_heading_start, len(text), label, len(segments)))
    return segments


def _label_for(segment: str) -> str:
    """Prefer clause/chapter headings over TOC noise (e.g. bare 附件1)."""
    lines = [raw.strip() for raw in segment.splitlines() if raw.strip()]
    if not lines:
        return "原文段落"

    def _clean(line: str) -> str:
        line = re.sub(r"^#+\s*", "", line)
        return line.strip()

    section_core = re.compile(
        r"^第[0-9一二三四五六七八九十百千]+[章节].{0,12}(成交|竞价|申报|撮合)"
    )
    section = re.compile(r"^第[0-9一二三四五六七八九十百千]+[章节]")
    clause_core = re.compile(
        r"^(\d+\.\d+(?:\.\d+)*)\s*.{0,30}"
        r"(成交价格|撮合成交|确定原则|集合竞价时|连续竞价时|最大成交量|价格优先)"
    )
    clause_any = re.compile(r"^(\d+\.\d+(?:\.\d+)*)\s*\S")
    preferred = re.compile(r"(第[0-9一二三四五六七八九十百千]+[章节条款]|[0-9]+(\.[0-9]+)+)")

    for raw in lines[:30]:
        line = _clean(raw)
        if clause_core.search(line):
            return line[:48] + ("…" if len(line) > 48 else "")
    for raw in lines[:20]:
        line = _clean(raw)
        if section_core.search(line) and len(line) >= 4:
            return line[:48] + ("…" if len(line) > 48 else "")
    for raw in lines[:20]:
        line = _clean(raw)
        if section.search(line) and len(line) >= 4:
            return line[:48] + ("…" if len(line) > 48 else "")
    for raw in lines[:12]:
        line = _clean(raw)
        if preferred.search(line) and len(line) >= 4:
            return line[:48] + ("…" if len(line) > 48 else "")
    for raw in lines[:12]:
        line = _clean(raw)
        if clause_any.search(line):
            return line[:48] + ("…" if len(line) > 48 else "")

    line = _clean(lines[0])
    return line[:48] + ("…" if len(line) > 48 else "")


def chunk_text(
    text: str | ParsedDocument,
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    spans: Iterable[Any] | None = None,
    page_spans: Iterable[Any] | None = None,
    section_spans: Iterable[Any] | None = None,
    clause_spans: Iterable[Any] | None = None,
) -> list[dict[str, Any]]:
    """Return chunks with original ranges and optional source anchors.

    The historical ``chunk_text(str, ...)`` call remains valid.  A
    ``ParsedDocument`` automatically supplies its page and section spans;
    callers may also pass span dictionaries/objects explicitly.
    """
    document = text if isinstance(text, ParsedDocument) else None
    source_text = document.text if document is not None else str(text or "")
    if not source_text or not source_text.strip():
        return []

    chunk_chars = max(MIN_CHUNK_CHARS, int(chunk_chars))
    overlap_chars = max(0, min(int(overlap_chars), chunk_chars // 3))
    all_extra = _normalise_spans(spans)
    pages = _normalise_spans(page_spans)
    sections = _normalise_spans(section_spans)
    clauses = _normalise_spans(clause_spans)
    if document is not None:
        pages = _normalise_spans([*document.page_spans, *pages])
        sections = _normalise_spans([*document.section_spans, *sections])
        clauses = _normalise_spans([*document.clause_spans, *clauses])
    # A generic span list is useful for old/simple integrations.  Infer its
    # role from whether it carries a page or section label.
    for span in all_extra:
        if span.get("page_start") is not None or span.get("page_end") is not None:
            pages.append(span)
        elif span.get("clause_ids") and not span.get("section"):
            clauses.append(span)
        else:
            sections.append(span)
    if not clauses:
        clauses = _clause_anchor_spans(source_text)

    segments = _document_segments(source_text, sections)
    raw_blocks: list[dict[str, Any]] = []

    for seg_start, seg_end, label, parent_index in segments:
        seg = source_text[seg_start:seg_end]
        if len(seg) <= chunk_chars:
            raw_blocks.append(
                {
                    "title": label,
                    "text": seg.strip(),
                    "start_char": seg_start + (len(seg) - len(seg.lstrip())),
                    "end_char": seg_start + len(seg.rstrip()),
                    "parent_index": parent_index,
                }
            )
            continue
        pos = 0
        local_i = 0
        while pos < len(seg):
            end = min(pos + chunk_chars, len(seg))
            if end < len(seg):
                window = seg[pos:end]
                br = max(window.rfind("\n"), window.rfind("。"), window.rfind("；"))
                if br >= chunk_chars // 2:
                    end = pos + br + 1
            piece = seg[pos:end]
            if piece.strip():
                abs_start = seg_start + pos
                lstrip = len(piece) - len(piece.lstrip())
                rstrip_len = len(piece.rstrip())
                piece_label = label if local_i == 0 and label else (_label_for(piece) or label)
                title = piece_label
                if local_i and title == label:
                    title = f"{piece_label} ({local_i + 1})"
                elif local_i and any(b.get("title") == title for b in raw_blocks[-3:]):
                    title = f"{piece_label} ({local_i + 1})"
                raw_blocks.append(
                    {
                        "title": title,
                        "text": piece.strip(),
                        "start_char": abs_start + lstrip,
                        "end_char": abs_start + rstrip_len,
                        "parent_index": parent_index,
                    }
                )
                local_i += 1
            if end >= len(seg):
                break
            pos = max(end - overlap_chars, pos + 1)

    chunks: list[dict[str, Any]] = []
    for i, block in enumerate(raw_blocks):
        if not block["text"]:
            continue
        title = block["title"] or f"原文块 #{i + 1}"
        item = {
            "chunk_index": i,
            "title": title,
            "text": block["text"],
            "start_char": int(block["start_char"]),
            "end_char": int(block["end_char"]),
            "parent_index": block.get("parent_index"),
        }
        item.update(
            _metadata_for_range(
                item["start_char"],
                item["end_char"],
                page_spans=pages,
                section_spans=sections,
                clause_spans=clauses,
            )
        )
        chunks.append(item)
    for i, chunk in enumerate(chunks):
        chunk["chunk_index"] = i
        if not chunk["title"]:
            chunk["title"] = f"原文块 #{i + 1}"
    return chunks
