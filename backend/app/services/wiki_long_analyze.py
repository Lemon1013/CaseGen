"""Long-source wiki analyze: window split + partial merge + orchestration."""

from __future__ import annotations

import re
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Callable, Optional

from app import config
from app.services.llm import LLMError
from app.services.wiki_candidates import (
    format_candidate_context,
    load_wiki_candidates_from_disk,
    recall_wiki_candidates,
)
from app.services.wiki_pages_parse import parse_json_flexible
from app.services.wiki_plan import (
    PlanValidationError,
    StepAPlan,
    coerce_step_a_plan,
    merge_step_a_plans,
    plan_to_legacy_analysis,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；\n])")
_HEADING_LINE = re.compile(
    r"^(#{1,6}\s+\S|第[0-9一二三四五六七八九十百千]+[章节]|[1-9]\d*(?:\.\d+){1,2}\s*\S)"
)

ChatFn = Callable[[list[dict[str, str]]], Any]

_WINDOW_SYSTEM_APPENDIX = (
    "\n\n【分窗分析】本轮只分析当前窗口正文。"
    "请输出严格 JSON，字段为 source_summary, digest_update, claims, entities, related_pages, "
    "contradictions, page_operations, review_items；page_operations 只能使用 "
    "create/update/noop，不能使用 merge。digest_update 只总结当前窗口新增事实；"
    "不要输出旧版兼容字段或其他额外字段。"
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

    # Explicit target (tests/small windows) may go below production default floor.
    if target_chars is None:
        target = max(1000, int(config.WIKI_ANALYZE_WINDOW_CHARS))
    else:
        target = max(200, int(target_chars))
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


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, dict):
                for key in ("name", "title", "statement", "claim", "rule", "text", "point", "hint"):
                    if item.get(key):
                        out.append(str(item[key]).strip())
                        break
                else:
                    t = str(item).strip()
                    if t:
                        out.append(t)
            else:
                t = str(item).strip()
                if t:
                    out.append(t)
        return [x for x in out if x]
    t = str(value).strip()
    return [t] if t else []


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        key = re.sub(r"\s+", " ", raw).strip()
        if not key:
            continue
        fold = key.casefold()
        if fold in seen:
            continue
        seen.add(fold)
        out.append(key)
    return out


def merge_analysis_partials(
    partials: list[dict[str, Any]],
    *,
    digest: str = "",
    source_chars: int = 0,
    max_rules: int = 80,
    max_other: int = 40,
) -> dict[str, Any]:
    if not partials:
        return {
            "summary_title": "",
            "key_rules": [],
            "api_points": [],
            "test_hints": [],
            "entities": [],
            "suggested_page_types": ["source_summary"],
            "global_digest": digest or "",
            "window_count": 0,
            "coverage": {"chars": source_chars, "windows": 0},
        }

    summary = ""
    for p in partials:
        t = str(p.get("summary_title") or "").strip()
        if t:
            summary = t
            break

    key_rules = _head_tail_items(
        _dedupe([r for p in partials for r in _as_str_list(p.get("key_rules"))]),
        max_rules,
    )
    api_points = _dedupe(
        [r for p in partials for r in _as_str_list(p.get("api_points"))]
    )[:max_other]
    test_hints = _dedupe(
        [r for p in partials for r in _as_str_list(p.get("test_hints"))]
    )[:max_other]
    entities = _dedupe([r for p in partials for r in _as_str_list(p.get("entities"))])[
        :max_other
    ]
    suggested = _dedupe(
        [r for p in partials for r in _as_str_list(p.get("suggested_page_types"))]
    )
    if not suggested:
        suggested = ["source_summary"]

    return {
        "summary_title": summary,
        "key_rules": key_rules,
        "api_points": api_points,
        "test_hints": test_hints,
        "entities": entities,
        "suggested_page_types": suggested,
        "global_digest": (digest or "").strip(),
        "window_count": len(partials),
        "coverage": {"chars": source_chars, "windows": len(partials)},
    }


def trim_digest(digest: str, max_chars: int | None = None) -> str:
    cap = int(max_chars or config.WIKI_ANALYZE_DIGEST_MAX)
    d = (digest or "").strip()
    if len(d) <= cap:
        return d
    head = max(1, cap // 2)
    marker = "\n…[digest middle truncated]…\n"
    tail = max(1, cap - head - len(marker))
    return d[:head].rstrip() + marker + d[-tail:].lstrip()


def _head_tail_items(items: list[str], maximum: int) -> list[str]:
    if maximum <= 0 or len(items) <= maximum:
        return items
    head = max(1, maximum // 2)
    return items[:head] + items[-(maximum - head):]


def heuristic_digest_append(digest: str, partial: dict[str, Any]) -> str:
    parts = [digest.strip()] if digest.strip() else []
    source_summary = partial.get("source_summary")
    summary_data = source_summary if isinstance(source_summary, Mapping) else {}
    title = str(partial.get("summary_title") or summary_data.get("title") or "").strip()
    if title:
        parts.append(f"## {title}")
    summary = str(summary_data.get("summary") or "").strip()
    if summary:
        parts.append(summary)
    rules = _as_str_list(partial.get("key_rules"))
    if not rules:
        rules = _as_str_list(partial.get("claims"))
    for rule in rules[:8]:
        parts.append(f"- {rule}")
    for ent in _as_str_list(partial.get("entities"))[:6]:
        parts.append(f"- 实体: {ent}")
    return trim_digest("\n".join(parts))


def _emit(
    on_step: Optional[Callable[..., None]],
    step: str,
    message: str,
    **extra: Any,
) -> None:
    if on_step is None:
        return
    try:
        on_step(step, message, **extra)
    except TypeError:
        on_step(step, message)


def _call_chat(chat_fn: ChatFn, messages: list[dict[str, str]]) -> str:
    result = chat_fn(messages)
    if isinstance(result, tuple):
        content = result[0]
    else:
        content = result
    if content is None or not str(content).strip():
        raise LLMError("Empty LLM content")
    return str(content)


def _read_wiki_contract(filename: str) -> str:
    for root in (config.WIKI_DIR, config.BUNDLED_WIKI_DIR):
        path = Path(root) / filename
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
    return ""


def _head_tail(text: str, maximum: int) -> str:
    text = str(text or "")
    if maximum <= 0 or len(text) <= maximum:
        return text
    head = max(1, maximum // 2)
    tail = max(1, maximum - head)
    return text[:head].rstrip() + "\n…[context truncated]…\n" + text[-tail:].lstrip()


def _window_anchor_context(window: Mapping[str, Any], source_windows: Iterable[Any] | None) -> list[dict[str, Any]]:
    base = int(window.get("start", 0) or 0)
    end = int(window.get("end", 0) or 0)
    result: list[dict[str, Any]] = []
    for raw in source_windows or ():
        item = dict(raw) if isinstance(raw, Mapping) else {}
        start = int(item.get("start", item.get("start_char", 0)) or 0)
        finish = int(item.get("end", item.get("end_char", 0)) or 0)
        if finish > base and start < end:
            item.setdefault("window_index", window.get("index"))
            result.append(item)
    if result:
        return result
    return [{"index": window.get("index"), "start": base, "end": end}]


def _plan_analysis(
    raw: Mapping[str, Any],
    *,
    source_path: str,
    source_windows: Iterable[Any] | None,
    existing_page_keys: Iterable[str] | None,
    source_length: int,
) -> StepAPlan:
    try:
        return coerce_step_a_plan(
            raw,
            source_path=source_path,
            source_windows=source_windows,
            existing_page_keys=existing_page_keys,
            source_length=source_length,
        )
    except PlanValidationError as exc:
        raise LLMError(f"invalid Wiki Step A plan: {exc}") from exc


def _analyze_once(
    *,
    chat_fn: ChatFn,
    system_prompt: str,
    user_content: str,
    retries: int | None = None,
) -> dict[str, Any]:
    """Call chat_fn and parse JSON analysis; retry on empty/parse failures."""
    max_retries = (
        int(config.WIKI_ANALYZE_WINDOW_RETRIES) if retries is None else int(retries)
    )
    attempts = max(1, max_retries + 1)
    last_err: Exception | None = None
    for _ in range(attempts):
        try:
            raw = _call_chat(
                chat_fn,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            return parse_json_flexible(raw)
        except (LLMError, ValueError, TypeError) as e:
            last_err = e
            continue
    raise LLMError(f"wiki analyze failed after {attempts} attempts: {last_err}")


def _single_user_prompt(
    text: str,
    *,
    source_path: str,
    filename: str,
    purpose: str = "",
    schema: str = "",
    candidate_context: str = "",
    source_anchor_context: str = "",
) -> str:
    meta_bits = []
    if source_path:
        meta_bits.append(f"source_path: {source_path}")
    if filename:
        meta_bits.append(f"filename: {filename}")
    header = "\n".join(meta_bits)
    if header:
        header += "\n\n"
    parts = [header.rstrip()]
    if purpose:
        parts.append("# WIKI PURPOSE\n" + purpose)
    if schema:
        parts.append("# WIKI SCHEMA\n" + schema)
    parts.append("# RELATED WIKI CANDIDATES\n" + (candidate_context or "（无）"))
    if source_anchor_context:
        parts.append("# SOURCE WINDOWS\n" + source_anchor_context)
    parts.append("# 原文\n" + text)
    return "\n\n".join(part for part in parts if part)


def _window_user_prompt(
    window: dict[str, Any],
    *,
    digest: str,
    source_path: str,
    filename: str,
    purpose: str = "",
    schema: str = "",
    candidate_context: str = "",
    source_anchor_context: str = "",
) -> str:
    parts: list[str] = []
    if source_path or filename:
        meta = []
        if source_path:
            meta.append(f"source_path: {source_path}")
        if filename:
            meta.append(f"filename: {filename}")
        parts.append("\n".join(meta))
    parts.append(
        f"# 窗口 {window['index']}/{window['total']}"
        f" (chars {window['start']}-{window['end']})"
    )
    hint = str(window.get("heading_hint") or "").strip()
    if hint:
        parts.append(f"heading_hint: {hint}")
    parts.append("# GLOBAL DIGEST\n" + (digest.strip() or "(empty)"))
    if purpose:
        parts.append("# WIKI PURPOSE\n" + purpose)
    if schema:
        parts.append("# WIKI SCHEMA\n" + schema)
    parts.append("# RELATED WIKI CANDIDATES\n" + (candidate_context or "（无）"))
    if source_anchor_context:
        parts.append("# SOURCE WINDOWS\n" + source_anchor_context)
    overlap = str(window.get("overlap_before") or "")
    if overlap.strip():
        parts.append("# OVERLAP\n" + overlap)
    parts.append("# MAIN\n" + str(window.get("main") or ""))
    return "\n\n".join(parts)


def run_long_source_analyze(
    text: str,
    *,
    chat_fn: ChatFn,
    analyze_system_prompt: str,
    source_path: str = "",
    filename: str = "",
    on_step: Optional[Callable[..., None]] = None,
    single_pass_chars: int | None = None,
    window_chars: int | None = None,
    overlap_chars: int | None = None,
    purpose: str | None = None,
    schema: str | None = None,
    candidate_pages: Iterable[Any] | None = None,
    source_windows: Iterable[Any] | None = None,
    existing_page_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Analyze source text in a single pass or multi-window mode.

    Returns: {mode: "single"|"multi", analysis: dict, window_count: int}
    """
    text = text or ""
    single_budget = int(
        single_pass_chars
        if single_pass_chars is not None
        else config.WIKI_ANALYZE_SINGLE_PASS_CHARS
    )
    win_chars = (
        int(window_chars)
        if window_chars is not None
        else int(config.WIKI_ANALYZE_WINDOW_CHARS)
    )
    ov_chars = (
        int(overlap_chars)
        if overlap_chars is not None
        else int(config.WIKI_ANALYZE_WINDOW_OVERLAP)
    )

    purpose_text = purpose if purpose is not None else _read_wiki_contract("purpose.md")
    schema_text = schema if schema is not None else _read_wiki_contract("schema.md")
    candidate_pages_were_provided = candidate_pages is not None
    if candidate_pages is None:
        candidate_pages = load_wiki_candidates_from_disk()
    else:
        candidate_pages = list(candidate_pages)
    recalled_candidates = recall_wiki_candidates(
        candidate_pages,
        text=text,
        filename=filename,
    )
    # A caller may already have performed a domain-specific recall and pass a
    # small candidate set. Keep that set visible if lexical extraction has no
    # usable term (for example, a short Chinese paragraph with no heading).
    prompt_candidates = recalled_candidates
    if candidate_pages_were_provided and not prompt_candidates:
        prompt_candidates = list(candidate_pages)[:80]
    candidate_context = format_candidate_context(prompt_candidates)
    candidate_keys = [str(item.get("page_key")) for item in candidate_pages if isinstance(item, Mapping) and item.get("page_key")]
    known_page_keys = list(existing_page_keys) if existing_page_keys is not None else candidate_keys

    def _do_single() -> dict[str, Any]:
        single_anchor_context = _window_anchor_context(
            {"index": 1, "start": 0, "end": len(text)},
            source_windows,
        )
        raw_analysis = _analyze_once(
            chat_fn=chat_fn,
            system_prompt=analyze_system_prompt,
            user_content=_single_user_prompt(
                text,
                source_path=source_path,
                filename=filename,
                purpose=purpose_text,
                schema=schema_text,
                candidate_context=candidate_context,
                source_anchor_context=json.dumps(
                    single_anchor_context,
                    ensure_ascii=False,
                ),
            ),
        )
        plan = _plan_analysis(
            raw_analysis,
            source_path=source_path,
            source_windows=single_anchor_context,
            existing_page_keys=known_page_keys or None,
            source_length=len(text),
        )
        analysis = plan_to_legacy_analysis(plan, raw_analysis)
        analysis["step_a_plan"] = plan.model_dump(mode="json")
        analysis["window_results"] = [
            {"index": 1, "start": 0, "end": len(text), "plan": plan.model_dump(mode="json")}
        ]
        _emit(
            on_step,
            "wiki_analyze",
            "single-pass analyze complete",
            mode="single",
            chars=len(text),
            source_path=source_path,
            filename=filename,
        )
        return {
            "mode": "single",
            "analysis": analysis,
            "plan": plan.model_dump(mode="json"),
            "step_a_plan": plan.model_dump(mode="json"),
            "window_results": analysis["window_results"],
            "window_count": 1,
        }

    if len(text) <= single_budget:
        return _do_single()

    windows = split_analyze_windows(
        text,
        target_chars=win_chars,
        overlap_chars=ov_chars,
    )
    if len(windows) <= 1:
        return _do_single()

    _emit(
        on_step,
        "wiki_analyze_plan",
        f"multi-window plan: {len(windows)} windows",
        window_count=len(windows),
        chars=len(text),
        window_chars=win_chars,
        overlap_chars=ov_chars,
        source_path=source_path,
        filename=filename,
    )

    system_multi = (analyze_system_prompt or "") + _WINDOW_SYSTEM_APPENDIX
    digest = ""
    partials: list[dict[str, Any]] = []
    window_plans: list[StepAPlan] = []
    window_results: list[dict[str, Any]] = []

    for w in windows:
        partial = _analyze_once(
            chat_fn=chat_fn,
            system_prompt=system_multi,
            user_content=_window_user_prompt(
                w,
                digest=digest,
                source_path=source_path,
                filename=filename,
                purpose=purpose_text,
                schema=schema_text,
                candidate_context=candidate_context,
                source_anchor_context=json.dumps(
                    _window_anchor_context(w, source_windows),
                    ensure_ascii=False,
                ),
            ),
        )
        plan = _plan_analysis(
            partial,
            source_path=source_path,
            source_windows=[*(_window_anchor_context(w, source_windows))],
            existing_page_keys=known_page_keys or None,
            source_length=len(text),
        )
        partials.append(partial)
        window_plans.append(plan)
        window_results.append(
            {
                "index": w["index"],
                "start": w["start"],
                "end": w["end"],
                "source_windows": _window_anchor_context(w, source_windows),
                "plan": plan.model_dump(mode="json"),
            }
        )
        update = str(partial.get("digest_update") or "").strip()
        if update:
            digest = trim_digest(
                (digest + "\n" + update).strip() if digest else update
            )
        else:
            digest = heuristic_digest_append(digest, partial)
        _emit(
            on_step,
            "wiki_analyze_window",
            f"window {w['index']}/{w['total']} analyzed",
            index=w["index"],
            total=w["total"],
            start=w["start"],
            end=w["end"],
            source_path=source_path,
            window_result=window_results[-1],
        )

    merged_plan = merge_step_a_plans(
        window_plans,
        source_windows=source_windows,
        existing_page_keys=known_page_keys or None,
        source_length=len(text),
    )
    merged = plan_to_legacy_analysis(merged_plan, partials[0] if partials else {})
    merged["global_digest"] = digest or merged.get("global_digest") or ""
    merged["window_count"] = len(partials)
    merged["coverage"] = {"chars": len(text), "windows": len(partials)}
    merged["window_results"] = window_results
    merged["step_a_plan"] = merged_plan.model_dump(mode="json")
    _emit(
        on_step,
        "wiki_analyze_consolidate",
        "merged multi-window analysis",
        window_count=len(partials),
        chars=len(text),
        source_path=source_path,
    )
    return {
        "mode": "multi",
        "analysis": merged,
        "plan": merged_plan.model_dump(mode="json"),
        "step_a_plan": merged_plan.model_dump(mode="json"),
        "window_results": window_results,
        "window_count": len(partials),
    }
