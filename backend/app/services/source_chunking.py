"""Split parsed document text into overlapping verbatim chunks."""

from __future__ import annotations

import re
from typing import Any


DEFAULT_CHUNK_CHARS = 1200
DEFAULT_OVERLAP_CHARS = 150
MIN_CHUNK_CHARS = 200


def _heading_or_blank_splits(text: str) -> list[tuple[int, int, str]]:
    """Return provisional segments (start, end, label) by blank lines / headings."""
    if not text:
        return []

    # Split on blank lines while keeping offsets
    parts: list[tuple[int, int, str]] = []
    pattern = re.compile(r"\n\s*\n+")
    last = 0
    for m in pattern.finditer(text):
        start, end = last, m.start()
        if end > start and text[start:end].strip():
            parts.append((start, end, _label_for(text[start:end])))
        last = m.end()
    if last < len(text) and text[last:].strip():
        parts.append((last, len(text), _label_for(text[last:])))
    if not parts and text.strip():
        parts.append((0, len(text), _label_for(text)))
    return parts


def _label_for(segment: str) -> str:
    """Prefer clause/chapter headings over TOC noise (e.g. bare 附件1)."""
    lines = [raw.strip() for raw in segment.splitlines() if raw.strip()]
    if not lines:
        return "原文段落"

    def _clean(line: str) -> str:
        line = re.sub(r"^#+\s*", "", line)
        return line.strip()

    # Prefer rule bodies (3.5.2 成交价格…) over earlier section headers in the window
    section_core = re.compile(
        r"^第[0-9一二三四五六七八九十百千]+[章节].{0,12}(成交|竞价|申报|撮合)"
    )
    section = re.compile(r"^第[0-9一二三四五六七八九十百千]+[章节]")
    # Avoid matching 未成交 / 全部成交 noise; prefer true rule heads like 3.5.2
    clause_core = re.compile(
        r"^(\d+\.\d+(?:\.\d+)*)\s*.{0,30}"
        r"(成交价格|撮合成交|确定原则|集合竞价时|连续竞价时|最大成交量|价格优先)"
    )
    clause_any = re.compile(r"^(\d+\.\d+(?:\.\d+)*)\s*\S")
    preferred = re.compile(
        r"(第[0-9一二三四五六七八九十百千]+[章节条款]|[0-9]+(\.[0-9]+)+)"
    )

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
    text: str,
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[dict[str, Any]]:
    """Return list of {chunk_index, title, text, start_char, end_char}."""
    if not text or not text.strip():
        return []

    chunk_chars = max(MIN_CHUNK_CHARS, int(chunk_chars))
    overlap_chars = max(0, min(int(overlap_chars), chunk_chars // 3))

    segments = _heading_or_blank_splits(text)
    raw_blocks: list[dict[str, Any]] = []

    for seg_start, seg_end, label in segments:
        seg = text[seg_start:seg_end]
        if len(seg) <= chunk_chars:
            raw_blocks.append(
                {
                    "title": label,
                    "text": seg.strip(),
                    "start_char": seg_start + (len(seg) - len(seg.lstrip())),
                    "end_char": seg_start + len(seg.rstrip()),
                }
            )
            continue
        # sliding window inside long segment
        pos = 0
        local_i = 0
        while pos < len(seg):
            end = min(pos + chunk_chars, len(seg))
            # prefer break at newline near end
            if end < len(seg):
                window = seg[pos:end]
                br = max(window.rfind("\n"), window.rfind("。"), window.rfind("；"))
                if br >= chunk_chars // 2:
                    end = pos + br + 1
            piece = seg[pos:end]
            if piece.strip():
                abs_start = seg_start + pos
                # trim leading whitespace offsets
                lstrip = len(piece) - len(piece.lstrip())
                rstrip_len = len(piece.rstrip())
                piece_label = _label_for(piece) or label
                # disambiguate repeated windows of same heading
                title = piece_label
                if local_i and title == label:
                    title = f"{piece_label} ({local_i + 1})"
                elif local_i and any(
                    b.get("title") == title for b in raw_blocks[-3:]
                ):
                    title = f"{piece_label} ({local_i + 1})"
                raw_blocks.append(
                    {
                        "title": title,
                        "text": piece.strip(),
                        "start_char": abs_start + lstrip,
                        "end_char": abs_start + rstrip_len,
                    }
                )
                local_i += 1
            if end >= len(seg):
                break
            pos = max(end - overlap_chars, pos + 1)

    chunks: list[dict[str, Any]] = []
    for i, b in enumerate(raw_blocks):
        if not b["text"]:
            continue
        chunks.append(
            {
                "chunk_index": i,
                "title": b["title"] or f"原文块 #{i + 1}",
                "text": b["text"],
                "start_char": int(b["start_char"]),
                "end_char": int(b["end_char"]),
            }
        )
    # reindex
    for i, c in enumerate(chunks):
        c["chunk_index"] = i
        if not c["title"]:
            c["title"] = f"原文块 #{i + 1}"
    return chunks
