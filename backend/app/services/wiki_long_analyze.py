"""Long-source wiki analyze: window split (merge/orchestration in later tasks)."""

from __future__ import annotations

import re
from typing import Any

from app import config

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；\n])")
_HEADING_LINE = re.compile(
    r"^(#{1,6}\s+\S|第[0-9一二三四五六七八九十百千]+[章节]|[1-9]\d*(?:\.\d+){1,2}\s*\S)"
)


def split_analyze_windows(
    text: str,
    *,
    target_chars: int | None = None,
    overlap_chars: int | None = None,
) -> list[dict[str, Any]]:
    """Split full document into analyze windows with coverage + overlap.

    Each item: {index, total, start, end, main, overlap_before, heading_hint}
    Invariant: mains cover [0, len(text)) without gaps; start/end are main spans.
    """
    text = text or ""
    if not text:
        return []

    target = max(1000, int(target_chars or config.WIKI_ANALYZE_WINDOW_CHARS))
    overlap = max(
        0,
        min(
            int(
                overlap_chars
                if overlap_chars is not None
                else config.WIKI_ANALYZE_WINDOW_OVERLAP
            ),
            target // 3,
        ),
    )

    blocks = _semantic_blocks(text, target)
    raw: list[tuple[int, int, str, str]] = []  # start, end, main, heading
    cur_parts: list[tuple[int, int, str]] = []
    cur_len = 0
    cur_heading = ""

    def flush() -> None:
        nonlocal cur_parts, cur_len, cur_heading
        if not cur_parts:
            return
        start = cur_parts[0][0]
        end = cur_parts[-1][1]
        main = text[start:end]
        raw.append((start, end, main, cur_heading))
        cur_parts = []
        cur_len = 0

    for b_start, b_end, b_text, heading in blocks:
        piece_len = b_end - b_start
        if cur_parts and cur_len + piece_len > target:
            flush()
        if not cur_parts:
            cur_heading = heading
        cur_parts.append((b_start, b_end, b_text))
        cur_len += piece_len
    flush()

    if not raw:
        return [
            {
                "index": 1,
                "total": 1,
                "start": 0,
                "end": len(text),
                "main": text,
                "overlap_before": "",
                "heading_hint": "",
            }
        ]

    windows: list[dict[str, Any]] = []
    total = len(raw)
    for i, (start, end, main, heading) in enumerate(raw):
        overlap_before = ""
        if i > 0 and overlap > 0:
            prev_main = raw[i - 1][2]
            overlap_before = _overlap_suffix(prev_main, overlap)
        windows.append(
            {
                "index": i + 1,
                "total": total,
                "start": start,
                "end": end,
                "main": main,
                "overlap_before": overlap_before,
                "heading_hint": heading,
            }
        )
    return windows


def _semantic_blocks(text: str, target: int) -> list[tuple[int, int, str, str]]:
    """Return (start, end, text, heading_hint) blocks covering full text."""
    lines = text.splitlines(keepends=True)
    blocks: list[tuple[int, int, str, str]] = []
    pos = 0
    buf: list[str] = []
    buf_start = 0
    heading = ""

    def flush_buf() -> None:
        nonlocal buf, buf_start
        if not buf:
            return
        chunk = "".join(buf)
        abs_start = buf_start
        for s, e, piece in _split_oversized(chunk, target):
            blocks.append((abs_start + s, abs_start + e, piece, heading))
        buf = []

    for line in lines:
        if _HEADING_LINE.match(line.strip()):
            flush_buf()
            heading = line.strip()[:80]
            line_start = pos
            blocks.append((line_start, line_start + len(line), line, heading))
            pos += len(line)
            buf_start = pos
            continue
        if not buf:
            buf_start = pos
        if line.strip() == "" and buf:
            buf.append(line)
            pos += len(line)
            flush_buf()
            buf_start = pos
            continue
        buf.append(line)
        pos += len(line)
    flush_buf()

    # cover any remainder if splitlines dropped final content without newline edge cases
    if not blocks and text:
        for s, e, piece in _split_oversized(text, target):
            blocks.append((s, e, piece, ""))
    # Ensure coverage: if blocks don't reach len(text), append tail
    if blocks and blocks[-1][1] < len(text):
        tail_s = blocks[-1][1]
        blocks.append((tail_s, len(text), text[tail_s:], heading))
    if not blocks and text:
        blocks.append((0, len(text), text, ""))
    return blocks


def _split_oversized(chunk: str, target: int) -> list[tuple[int, int, str]]:
    if len(chunk) <= int(target * 1.25):
        return [(0, len(chunk), chunk)]
    out: list[tuple[int, int, str]] = []
    parts = _SENTENCE_SPLIT.split(chunk)
    cur = ""
    cur_start = 0
    offset = 0
    for part in parts:
        if not part:
            continue
        if cur and len(cur) + len(part) > target:
            out.append((cur_start, cur_start + len(cur), cur))
            cur_start = offset
            cur = ""
        if not cur:
            cur_start = offset
        if len(part) > target:
            if cur:
                out.append((cur_start, cur_start + len(cur), cur))
                cur = ""
            for i in range(0, len(part), target):
                sl = part[i : i + target]
                out.append((offset + i, offset + i + len(sl), sl))
            cur_start = offset + len(part)
        else:
            cur += part
        offset += len(part)
    if cur:
        out.append((cur_start, cur_start + len(cur), cur))
    return out or [(0, len(chunk), chunk)]


def _overlap_suffix(text: str, max_chars: int) -> str:
    if max_chars <= 0 or not text:
        return ""
    if len(text) <= max_chars:
        return text
    slice_ = text[-max_chars:]
    # prefer break after newline / sentence in the slice
    for sep in ("\n\n", "\n", "。", "；"):
        idx = slice_.find(sep)
        if idx != -1 and idx + len(sep) < len(slice_) - 20:
            return slice_[idx + len(sep) :]
    return slice_
