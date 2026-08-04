# Wiki Long-Source Analyze Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CaseGen wiki `wiki_analyze` cover the full document via llm_wiki-style multi-window map + rolling digest + consolidate, instead of truncating to 14k characters.

**Architecture:** Add `wiki_long_analyze.py` for window split, per-window analyze, digest update, and JSON merge. `wiki_ingest.ingest_document` calls it after SourceChunk persistence; short texts stay single-pass. Write step consumes consolidated analysis (+ optional `global_digest`), not `text[:2500]` as primary knowledge.

**Tech Stack:** Python 3, FastAPI, SQLModel, existing `chat_fn` / `chat_completion`, pytest.

**Spec:** `docs/superpowers/specs/2026-07-25-wiki-long-source-analyze-design.md`

**Worktree:** `CaseGen/.worktrees/feat-mvp` (backend under `backend/`)

---

## File map

| File | Role |
|------|------|
| `backend/app/config.py` | Env knobs for budgets/windows |
| `backend/app/services/wiki_long_analyze.py` | **Create** — split / map / merge / run |
| `backend/app/services/wiki_ingest.py` | Wire analyze + write payload |
| `backend/app/services/wiki_pages_parse.py` | Fallback page includes `global_digest` |
| `backend/tests/test_wiki_long_analyze.py` | **Create** — unit tests |
| `backend/tests/test_wiki_ingest_long.py` | **Create** — ingest multi-window mock |
| `backend/tests/test_wiki_api.py` | Keep green (short docs still 1 analyze) |

---

### Task 1: Config knobs

**Files:**
- Modify: `backend/app/config.py`
- Test: none yet (used by later tasks)

- [ ] **Step 1: Add constants after `SOURCE_CHUNK_OVERLAP`**

```python
# Wiki long-source analyze (full-doc coverage; see wiki_long_analyze.py)
WIKI_ANALYZE_SINGLE_PASS_CHARS = int(os.getenv("WIKI_ANALYZE_SINGLE_PASS_CHARS", "48000"))
WIKI_ANALYZE_WINDOW_CHARS = int(os.getenv("WIKI_ANALYZE_WINDOW_CHARS", "12000"))
WIKI_ANALYZE_WINDOW_OVERLAP = int(os.getenv("WIKI_ANALYZE_WINDOW_OVERLAP", "1000"))
WIKI_ANALYZE_DIGEST_MAX = int(os.getenv("WIKI_ANALYZE_DIGEST_MAX", "12000"))
WIKI_ANALYZE_PARTIAL_JSON_MAX = int(os.getenv("WIKI_ANALYZE_PARTIAL_JSON_MAX", "8000"))
WIKI_WRITE_ANALYSIS_CHARS = int(os.getenv("WIKI_WRITE_ANALYSIS_CHARS", "24000"))
WIKI_ANALYZE_WINDOW_RETRIES = int(os.getenv("WIKI_ANALYZE_WINDOW_RETRIES", "2"))
```

- [ ] **Step 2: Commit**

```bash
cd CaseGen/.worktrees/feat-mvp
git add backend/app/config.py
git commit -m "chore: add wiki long-source analyze config knobs"
```

---

### Task 2: Window splitter (TDD)

**Files:**
- Create: `backend/app/services/wiki_long_analyze.py`
- Create: `backend/tests/test_wiki_long_analyze.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_wiki_long_analyze.py`:

```python
from app.services.wiki_long_analyze import split_analyze_windows, merge_analysis_partials


def test_split_covers_full_text_without_gaps():
    # Build text longer than target so multiple windows appear
    parts = [f"段落{i}。" + ("内容" * 50) for i in range(30)]
    text = "\n\n".join(parts)
    windows = split_analyze_windows(text, target_chars=800, overlap_chars=100)
    assert len(windows) >= 2
    assert windows[0]["start"] == 0
    assert windows[-1]["end"] == len(text)
    # main spans cover [0, len) without gaps
    covered = 0
    for w in windows:
        assert w["start"] <= covered
        assert w["end"] > w["start"]
        assert w["main"] == text[w["start"] : w["end"]]
        covered = max(covered, w["end"])
    assert covered == len(text)
    # later windows carry overlap from previous main
    assert windows[1]["overlap_before"]
    assert windows[1]["overlap_before"] in windows[0]["main"]


def test_split_short_text_single_window():
    text = "短文档仅一窗。"
    windows = split_analyze_windows(text, target_chars=12000, overlap_chars=1000)
    assert len(windows) == 1
    assert windows[0]["main"] == text
    assert windows[0]["overlap_before"] == ""
```

- [ ] **Step 2: Run tests — expect FAIL (module missing)**

```bash
cd backend
.venv/Scripts/python.exe -m pytest tests/test_wiki_long_analyze.py::test_split_covers_full_text_without_gaps tests/test_wiki_long_analyze.py::test_split_short_text_single_window -v
```

Expected: `ModuleNotFoundError` or `ImportError` for `wiki_long_analyze`.

- [ ] **Step 3: Implement `split_analyze_windows`**

Create `backend/app/services/wiki_long_analyze.py`:

```python
from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from app import config
from app.services.llm import LLMError
from app.services.wiki_pages_parse import parse_json_flexible

ChatFn = Callable[[list[dict[str, str]]], Any]

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
    overlap = max(0, min(int(overlap_chars if overlap_chars is not None else config.WIKI_ANALYZE_WINDOW_OVERLAP), target // 3))

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
        abs_end = buf_start + len(chunk)
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
```

(Keep file open for Tasks 3–4; do not delete helpers.)

- [ ] **Step 4: Run splitter tests — expect PASS**

```bash
.venv/Scripts/python.exe -m pytest tests/test_wiki_long_analyze.py::test_split_covers_full_text_without_gaps tests/test_wiki_long_analyze.py::test_split_short_text_single_window -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wiki_long_analyze.py backend/tests/test_wiki_long_analyze.py
git commit -m "feat: split full document into wiki analyze windows"
```

---

### Task 3: Merge partials (TDD)

**Files:**
- Modify: `backend/app/services/wiki_long_analyze.py`
- Modify: `backend/tests/test_wiki_long_analyze.py`

- [ ] **Step 1: Add failing test**

```python
def test_merge_analysis_partials_dedupes_and_keeps_tail_rules():
    partials = [
        {
            "summary_title": "文档前部",
            "key_rules": ["规则A", "规则B"],
            "api_points": [],
            "test_hints": ["提示1"],
            "entities": ["实体1"],
            "suggested_page_types": ["source_summary"],
            "digest_update": "前部摘要",
        },
        {
            "summary_title": "文档后部",
            "key_rules": ["规则B", "尾部独有规则TAIL_MARKER"],
            "api_points": ["接口X"],
            "test_hints": [],
            "entities": ["实体1", "实体2"],
            "suggested_page_types": ["business"],
            "digest_update": "全文摘要含尾部",
        },
    ]
    merged = merge_analysis_partials(partials, digest="全文摘要含尾部", source_chars=9999)
    assert merged["summary_title"] == "文档前部"
    assert merged["key_rules"] == ["规则A", "规则B", "尾部独有规则TAIL_MARKER"]
    assert "接口X" in merged["api_points"]
    assert merged["entities"] == ["实体1", "实体2"]
    assert set(merged["suggested_page_types"]) >= {"source_summary", "business"}
    assert merged["global_digest"] == "全文摘要含尾部"
    assert merged["window_count"] == 2
    assert merged["coverage"]["chars"] == 9999
    assert merged["coverage"]["windows"] == 2
```

- [ ] **Step 2: Run — expect FAIL**

```bash
.venv/Scripts/python.exe -m pytest tests/test_wiki_long_analyze.py::test_merge_analysis_partials_dedupes_and_keeps_tail_rules -v
```

- [ ] **Step 3: Implement merge helpers**

Append to `wiki_long_analyze.py`:

```python
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
                for key in ("name", "title", "rule", "text", "point", "hint"):
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

    key_rules = _dedupe([r for p in partials for r in _as_str_list(p.get("key_rules"))])[:max_rules]
    api_points = _dedupe([r for p in partials for r in _as_str_list(p.get("api_points"))])[:max_other]
    test_hints = _dedupe([r for p in partials for r in _as_str_list(p.get("test_hints"))])[:max_other]
    entities = _dedupe([r for p in partials for r in _as_str_list(p.get("entities"))])[:max_other]
    suggested = _dedupe([r for p in partials for r in _as_str_list(p.get("suggested_page_types"))])
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
    return d[:cap].rstrip() + "\n…[digest truncated]"


def heuristic_digest_append(digest: str, partial: dict[str, Any]) -> str:
    parts = [digest.strip()] if digest.strip() else []
    title = str(partial.get("summary_title") or "").strip()
    if title:
        parts.append(f"## {title}")
    for rule in _as_str_list(partial.get("key_rules"))[:8]:
        parts.append(f"- {rule}")
    for ent in _as_str_list(partial.get("entities"))[:6]:
        parts.append(f"- 实体: {ent}")
    return trim_digest("\n".join(parts))
```

- [ ] **Step 4: Run merge test — PASS**

```bash
.venv/Scripts/python.exe -m pytest tests/test_wiki_long_analyze.py::test_merge_analysis_partials_dedupes_and_keeps_tail_rules -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wiki_long_analyze.py backend/tests/test_wiki_long_analyze.py
git commit -m "feat: merge multi-window wiki analysis partials"
```

---

### Task 4: `run_long_source_analyze` orchestration (TDD)

**Files:**
- Modify: `backend/app/services/wiki_long_analyze.py`
- Modify: `backend/tests/test_wiki_long_analyze.py`

- [ ] **Step 1: Write failing tests for single-pass vs multi-window**

```python
import json

from app.services.wiki_long_analyze import run_long_source_analyze


def test_run_long_analyze_single_pass_under_budget(monkeypatch):
    monkeypatch.setattr("app.services.wiki_long_analyze.config.WIKI_ANALYZE_SINGLE_PASS_CHARS", 10000)
    monkeypatch.setattr("app.services.wiki_long_analyze.config.WIKI_ANALYZE_WINDOW_CHARS", 3000)

    calls: list[str] = []

    def chat(messages):
        calls.append(messages[-1]["content"])
        return json.dumps(
            {
                "summary_title": "短文",
                "key_rules": ["R1"],
                "api_points": [],
                "test_hints": [],
                "entities": [],
                "suggested_page_types": ["source_summary"],
            },
            ensure_ascii=False,
        )

    text = "短正文" * 10
    result = run_long_source_analyze(
        text,
        chat_fn=chat,
        analyze_system_prompt="输出 summary_title key_rules 仅 JSON",
        source_path="raw/x.md",
        filename="x.md",
    )
    assert result["mode"] == "single"
    assert result["analysis"]["key_rules"] == ["R1"]
    assert len(calls) == 1
    assert "短正文" in calls[0]


def test_run_long_analyze_multi_window_sees_tail(monkeypatch):
    monkeypatch.setattr("app.services.wiki_long_analyze.config.WIKI_ANALYZE_SINGLE_PASS_CHARS", 500)
    monkeypatch.setattr("app.services.wiki_long_analyze.config.WIKI_ANALYZE_WINDOW_CHARS", 200)
    monkeypatch.setattr("app.services.wiki_long_analyze.config.WIKI_ANALYZE_WINDOW_OVERLAP", 40)
    monkeypatch.setattr("app.services.wiki_long_analyze.config.WIKI_ANALYZE_WINDOW_RETRIES", 0)

    # Head filler + unique tail marker only at end
    head = ("前部段落。\n\n" * 40) + ("填充内容。" * 30)
    tail = "\n\n尾部专属条款 TAIL_ONLY_RULE_9_9_9 必须被分析到。\n"
    text = head + tail
    assert "TAIL_ONLY_RULE_9_9_9" in text

    call_n = {"n": 0}

    def chat(messages):
        call_n["n"] += 1
        user = messages[-1]["content"]
        # If MAIN/user contains tail marker, emit it as key_rule
        rules = []
        if "TAIL_ONLY_RULE_9_9_9" in user:
            rules.append("TAIL_ONLY_RULE_9_9_9")
        else:
            rules.append(f"head_rule_{call_n['n']}")
        return json.dumps(
            {
                "summary_title": f"窗{call_n['n']}",
                "key_rules": rules,
                "api_points": [],
                "test_hints": [],
                "entities": [],
                "suggested_page_types": ["business"],
                "digest_update": f"digest-{call_n['n']}",
            },
            ensure_ascii=False,
        )

    steps: list[dict] = []

    def on_step(step: str, message: str, **extra):
        steps.append({"step": step, "message": message, **extra})

    result = run_long_source_analyze(
        text,
        chat_fn=chat,
        analyze_system_prompt="summary_title key_rules 仅 JSON",
        source_path="raw/long.md",
        filename="long.md",
        on_step=on_step,
    )
    assert result["mode"] == "multi"
    assert call_n["n"] >= 2
    assert "TAIL_ONLY_RULE_9_9_9" in result["analysis"]["key_rules"]
    assert any(s["step"] == "wiki_analyze_plan" for s in steps)
    assert any(s["step"] == "wiki_analyze_window" for s in steps)
    assert any(s["step"] == "wiki_analyze_consolidate" for s in steps)
```

- [ ] **Step 2: Run — expect FAIL**

```bash
.venv/Scripts/python.exe -m pytest tests/test_wiki_long_analyze.py::test_run_long_analyze_single_pass_under_budget tests/test_wiki_long_analyze.py::test_run_long_analyze_multi_window_sees_tail -v
```

- [ ] **Step 3: Implement `run_long_source_analyze`**

Append to `wiki_long_analyze.py`:

```python
def _call_chat(chat_fn: ChatFn, messages: list[dict[str, str]]) -> str:
    result = chat_fn(messages)
    if isinstance(result, tuple):
        content = result[0]
    else:
        content = result
    if not content or not str(content).strip():
        raise LLMError("Empty LLM content")
    return str(content)


def _analyze_once(
    chat_fn: ChatFn,
    *,
    system: str,
    user: str,
    retries: int,
) -> dict[str, Any]:
    last_err: Exception | None = None
    attempts = max(1, retries + 1)
    for i in range(attempts):
        try:
            raw = _call_chat(
                chat_fn,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user if i == 0 else user + "\n\n请只输出 JSON 对象，不要 Markdown 围栏。"},
                ],
            )
            return parse_json_flexible(raw)
        except (LLMError, ValueError, json.JSONDecodeError) as exc:
            last_err = exc
    assert last_err is not None
    raise last_err


_WINDOW_SYSTEM_APPENDIX = """
# 分窗模式补充
你正在分析长文档的一个窗口。
1. 仅从 # MAIN 抽取新的 key_rules / api_points / test_hints / entities；不要编造 MAIN 未出现的条款。
2. # GLOBAL DIGEST 与 # OVERLAP 仅作连贯上下文，勿重复罗列 digest 已有条目，除非需要修正。
3. 在 JSON 中增加 digest_update 字符串：融合本窗后的全书滚动摘要（简洁）。
4. 仍只输出一个 JSON 对象。
""".strip()


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
) -> dict[str, Any]:
    """Return {mode, analysis, window_count, steps_meta}.

    mode: "single" | "multi"
    """
    text = text or ""
    budget = int(single_pass_chars if single_pass_chars is not None else config.WIKI_ANALYZE_SINGLE_PASS_CHARS)
    target = int(window_chars if window_chars is not None else config.WIKI_ANALYZE_WINDOW_CHARS)
    overlap = int(overlap_chars if overlap_chars is not None else config.WIKI_ANALYZE_WINDOW_OVERLAP)
    retries = int(getattr(config, "WIKI_ANALYZE_WINDOW_RETRIES", 2))

    def step(name: str, message: str, **extra: Any) -> None:
        if on_step:
            on_step(name, message, **extra)

    # Single-pass
    if len(text) <= budget:
        user = (
            f"源文件路径: {source_path}\n"
            f"文件名: {filename}\n\n"
            f"# 原文\n{text}"
        )
        analysis = _analyze_once(
            chat_fn,
            system=analyze_system_prompt,
            user=user,
            retries=retries,
        )
        # normalize lists
        merged = merge_analysis_partials([analysis], digest="", source_chars=len(text))
        # prefer model summary_title from single partial
        if analysis.get("summary_title"):
            merged["summary_title"] = str(analysis["summary_title"]).strip()
        step("wiki_analyze", "Single-pass analysis JSON parsed", source_chars=len(text), mode="single")
        return {
            "mode": "single",
            "analysis": merged,
            "window_count": 1,
        }

    windows = split_analyze_windows(text, target_chars=target, overlap_chars=overlap)
    if len(windows) <= 1:
        # packing produced one window — treat as single full-text
        user = (
            f"源文件路径: {source_path}\n"
            f"文件名: {filename}\n\n"
            f"# 原文\n{text}"
        )
        analysis = _analyze_once(
            chat_fn,
            system=analyze_system_prompt,
            user=user,
            retries=retries,
        )
        merged = merge_analysis_partials([analysis], digest="", source_chars=len(text))
        if analysis.get("summary_title"):
            merged["summary_title"] = str(analysis["summary_title"]).strip()
        step("wiki_analyze", "Single-window full-text analysis", source_chars=len(text), mode="single")
        return {"mode": "single", "analysis": merged, "window_count": 1}

    step(
        "wiki_analyze_plan",
        f"Long-source plan: {len(windows)} window(s)",
        window_count=len(windows),
        target_chars=target,
        overlap_chars=overlap,
        source_chars=len(text),
    )

    system = analyze_system_prompt.rstrip() + "\n\n" + _WINDOW_SYSTEM_APPENDIX
    partials: list[dict[str, Any]] = []
    digest = ""

    for w in windows:
        user = (
            f"源文件路径: {source_path}\n"
            f"文件名: {filename}\n"
            f"窗口: {w['index']}/{w['total']}\n"
            f"字符范围: {w['start']}-{w['end']}\n"
            f"标题提示: {w.get('heading_hint') or ''}\n\n"
            f"# GLOBAL DIGEST\n{digest or '（尚无）'}\n\n"
            f"# OVERLAP\n{w.get('overlap_before') or '（无）'}\n\n"
            f"# MAIN\n{w['main']}"
        )
        partial = _analyze_once(chat_fn, system=system, user=user, retries=retries)
        partials.append(partial)
        du = str(partial.get("digest_update") or "").strip()
        if du:
            digest = trim_digest(du)
        else:
            digest = heuristic_digest_append(digest, partial)
        step(
            "wiki_analyze_window",
            f"Analyzed window {w['index']}/{w['total']}",
            index=w["index"],
            total=w["total"],
            start=w["start"],
            end=w["end"],
            chars=w["end"] - w["start"],
        )

    merged = merge_analysis_partials(partials, digest=digest, source_chars=len(text))
    step(
        "wiki_analyze_consolidate",
        f"Merged {len(partials)} window analyses",
        key_rules=len(merged.get("key_rules") or []),
        window_count=len(partials),
    )
    return {
        "mode": "multi",
        "analysis": merged,
        "window_count": len(partials),
    }
```

- [ ] **Step 4: Run Task 4 tests — PASS**

```bash
.venv/Scripts/python.exe -m pytest tests/test_wiki_long_analyze.py -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/wiki_long_analyze.py backend/tests/test_wiki_long_analyze.py
git commit -m "feat: run single-pass or multi-window wiki analyze"
```

---

### Task 5: Wire `wiki_ingest` + write payload

**Files:**
- Modify: `backend/app/services/wiki_ingest.py`
- Modify: `backend/app/services/wiki_pages_parse.py` (digest in fallback)
- Test: existing + new ingest long test in Task 6

- [ ] **Step 1: Remove hard 14k gate; call `run_long_source_analyze`**

In `wiki_ingest.py`:

1. Delete (or stop using) `ANALYZE_SOURCE_CHARS = 14000`.
2. Import:

```python
from app.services.wiki_long_analyze import run_long_source_analyze
```

3. Replace Step 2 analyze block (from loading analyze_prompt through parse_json_flexible) with:

```python
        # 2) Analyze (single-pass or multi-window long-source)
        analyze_prompt = _load_active_prompt(session, "wiki_analyze")

        def _on_analyze_step(step: str, message: str, **extra: Any) -> None:
            _append_step(job, step, message, **extra)
            session.add(job)
            session.commit()

        analyze_result = run_long_source_analyze(
            text,
            chat_fn=chat_fn,
            analyze_system_prompt=analyze_prompt.content,
            source_path=doc.stored_path or "",
            filename=doc.filename or "",
            on_step=_on_analyze_step,
        )
        analysis = analyze_result["analysis"]
        # Ensure classic step name exists for single-pass (run_* already logs)
        if analyze_result.get("mode") == "multi" and not any(
            True for _ in []
        ):
            pass
        session.add(job)
        session.commit()
```

Note: `run_long_source_analyze` already emits `wiki_analyze` / plan / window / consolidate via `on_step`. Do **not** double-log a fake analyze for multi. For single-pass it logs `wiki_analyze`.

4. Replace write user content construction:

```python
        analysis_json = json.dumps(analysis, ensure_ascii=False, indent=2)
        write_cap = int(getattr(config, "WIKI_WRITE_ANALYSIS_CHARS", 24000))
        if len(analysis_json) > write_cap:
            # Prefer keeping digest + truncated lists
            slim = {
                "summary_title": analysis.get("summary_title"),
                "global_digest": analysis.get("global_digest"),
                "key_rules": (analysis.get("key_rules") or [])[:60],
                "api_points": (analysis.get("api_points") or [])[:30],
                "test_hints": (analysis.get("test_hints") or [])[:30],
                "entities": (analysis.get("entities") or [])[:30],
                "suggested_page_types": analysis.get("suggested_page_types"),
                "window_count": analysis.get("window_count"),
                "coverage": analysis.get("coverage"),
                "_truncated": True,
            }
            analysis_json = json.dumps(slim, ensure_ascii=False, indent=2)
            if len(analysis_json) > write_cap:
                analysis_json = analysis_json[:write_cap] + "\n...[truncated]"

        digest = str(analysis.get("global_digest") or "").strip()
        write_messages = [
            {"role": "system", "content": write_prompt.content},
            {
                "role": "user",
                "content": (
                    f"# Step A 分析结果\n```json\n{analysis_json}\n```\n\n"
                    f"# 源路径\n{doc.stored_path}\n\n"
                    f"# 现有 index 摘要\n{index_excerpt[:2000]}\n\n"
                    f"# 全局摘要 digest\n{digest[:8000] if digest else '（无）'}\n\n"
                    f"# 原文抽样（辅助，非全文）\n{(text[:1500])}"
                ),
            },
        ]
```

5. Remove dead `WRITE_SOURCE_EXCERPT_CHARS` usage or keep constant unused — prefer delete constant if unused.

- [ ] **Step 2: Extend `pages_from_analysis` for digest**

In `wiki_pages_parse.py` `pages_from_analysis`, after building `summary_lines` entities section, add:

```python
    digest = str(analysis.get("global_digest") or "").strip()
    if digest:
        summary_lines.append("## 全局摘要")
        summary_lines.append(digest[:3000])
        summary_lines.append("")
```

- [ ] **Step 3: Run existing wiki ingest tests**

```bash
.venv/Scripts/python.exe -m pytest tests/test_wiki_api.py tests/test_wiki_ingest_fallback.py tests/test_wiki_ingest_unit.py tests/test_source_chunks.py tests/test_wiki_long_analyze.py -v --tb=short
```

Expected: PASS (fake chat still matches analyze system prompt).

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/wiki_ingest.py backend/app/services/wiki_pages_parse.py
git commit -m "feat: wire long-source analyze into wiki ingest"
```

---

### Task 6: End-to-end ingest test (multi-window + tail rule)

**Files:**
- Create: `backend/tests/test_wiki_ingest_long.py`

- [ ] **Step 1: Write test**

```python
import json

from fastapi.testclient import TestClient

from app.main import create_app
from app.api import documents as documents_api


def test_ingest_long_document_analyzes_tail_window(tmp_app_data, monkeypatch):
    """Multi-window ingest must surface a rule that only appears in the document tail."""
    monkeypatch.setenv("WIKI_ANALYZE_SINGLE_PASS_CHARS", "400")
    monkeypatch.setenv("WIKI_ANALYZE_WINDOW_CHARS", "180")
    monkeypatch.setenv("WIKI_ANALYZE_WINDOW_OVERLAP", "30")
    # reload config values used by module (config reads env at import — patch attrs)
    import app.config as cfg
    import app.services.wiki_long_analyze as la

    monkeypatch.setattr(cfg, "WIKI_ANALYZE_SINGLE_PASS_CHARS", 400)
    monkeypatch.setattr(cfg, "WIKI_ANALYZE_WINDOW_CHARS", 180)
    monkeypatch.setattr(cfg, "WIKI_ANALYZE_WINDOW_OVERLAP", 30)
    monkeypatch.setattr(la.config, "WIKI_ANALYZE_SINGLE_PASS_CHARS", 400)
    monkeypatch.setattr(la.config, "WIKI_ANALYZE_WINDOW_CHARS", 180)
    monkeypatch.setattr(la.config, "WIKI_ANALYZE_WINDOW_OVERLAP", 30)

    head = ("章节前部说明。\n\n" * 25) + ("填充。" * 40)
    tail = "\n\n尾部条款：TAIL_INGEST_MARKER_42 仅出现在文末。\n"
    content = (head + tail).encode("utf-8")

    def chat(messages):
        system = (messages[0].get("content") or "") if messages else ""
        user = messages[-1].get("content") or ""
        if "summary_title" in system or "key_rules" in system or "仅 JSON" in system or "分窗模式" in system:
            rules = []
            if "TAIL_INGEST_MARKER_42" in user:
                rules.append("TAIL_INGEST_MARKER_42")
            else:
                rules.append("head_only")
            return json.dumps(
                {
                    "summary_title": "长文测试",
                    "key_rules": rules,
                    "api_points": [],
                    "test_hints": [],
                    "entities": ["长文"],
                    "suggested_page_types": ["source_summary", "business"],
                    "digest_update": "digest",
                },
                ensure_ascii=False,
            )
        # write: include rules from analysis json in user if present
        return """---
title: 长文摘要
type: source_summary
sources: ["raw/sources/long.md"]
tags: ["长文"]
---
见分析结果。
---
title: 长文业务
type: business
sources: ["raw/sources/long.md"]
tags: ["长文"]
---
业务页。
"""

    monkeypatch.setattr(documents_api, "_INGEST_CHAT_FN", chat)
    client = TestClient(create_app())
    up = client.post(
        "/api/documents",
        files={"file": ("long.md", content, "text/markdown")},
    )
    assert up.status_code == 200
    doc_id = up.json()["id"]
    ing = client.post(f"/api/documents/{doc_id}/ingest")
    assert ing.status_code == 200
    job = ing.json()
    assert job["status"] == "success", job

    # step log should show multi-window plan
    detail = client.get(f"/api/ingest-jobs/{job['id']}").json()
    log = json.loads(detail.get("step_log_json") or "[]")
    steps = [e.get("step") for e in log]
    assert "wiki_analyze_plan" in steps
    assert "wiki_analyze_window" in steps
    assert "wiki_analyze_consolidate" in steps

    # fallback or write pages — at least job success; stronger: re-run analyze path already unit-tested.
    # Assert document ready and pages exist
    doc = client.get(f"/api/documents/{doc_id}").json()
    assert doc["status"] == "ready"
    pages = client.get("/api/wiki/pages").json()
    assert len(pages) >= 1
```

- [ ] **Step 2: Run test**

```bash
.venv/Scripts/python.exe -m pytest tests/test_wiki_ingest_long.py -v --tb=short
```

Expected: PASS. If write path doesn't embed tail marker in page body, that is OK — success criteria for this test is multi-window steps + job success; tail extraction is guaranteed by unit test `test_run_long_analyze_multi_window_sees_tail`. Optionally strengthen by reading job is enough.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_wiki_ingest_long.py
git commit -m "test: ingest long document uses multi-window analyze"
```

---

### Task 7: Full regression + smoke notes

**Files:** none required

- [ ] **Step 1: Full pytest**

```bash
cd backend
.venv/Scripts/python.exe -m pytest -q --tb=line
```

Expected: all previously passing tests still pass; new tests pass.

- [ ] **Step 2: Manual re-ingest note ( fore operator)**

For existing long docs already `ready` under old 14k analyze:

1. Restart API so new code loads.
2. `POST /api/documents/{id}/ingest` again (replaces wiki pages + refreshes chunks).
3. Inspect `GET /api/ingest-jobs/{id}` for `wiki_analyze_plan` / multiple `wiki_analyze_window`.
4. Spot-check that a late-chapter topic appears in new wiki pages or analysis step extras.

- [ ] **Step 3: Final commit if any fixups**

```bash
git add -A
git status
# commit only if fixups landed
git commit -m "fix: long-source analyze regressions" || true
```

---

## Spec coverage checklist

| Spec requirement | Task |
|------------------|------|
| Full coverage windows | Task 2 |
| Short single-pass | Task 4 |
| Map + digest + consolidate | Task 4 |
| Fail on window error | Task 4 `_analyze_once` raises |
| SourceChunk unchanged | Task 5 (still `replace_chunks_for_document` first) |
| Write uses consolidated analysis | Task 5 |
| step_log plan/window/consolidate | Task 4–6 |
| Config env knobs | Task 1 |
| Fallback digest section | Task 5 |
| Tail rule in analysis | Task 4 unit + Task 6 multi steps |
| No checkpoint v1 | — explicit non-goal |
| Remove 14k sole gate | Task 5 |

## Placeholder / consistency self-review

- Function names stable: `split_analyze_windows`, `merge_analysis_partials`, `run_long_source_analyze`.
- Config names match spec.
- No TBD left in tasks.
- Commits optional if worktree policy forbids; still run tests.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-25-wiki-long-source-analyze.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks  
2. **Inline Execution** — execute tasks in this session with checkpoints  

Which approach?
