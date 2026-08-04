# Design: Wiki Long-Source Analyze (llm_wiki-aligned)

**Date:** 2026-07-25  
**Status:** Draft for approval  
**Repo path:** CaseGen `.worktrees/feat-mvp`  
**Reference:** [nashsu/llm_wiki](https://github.com/nashsu/llm_wiki) `src/lib/ingest.ts` long-source path  

## Problem

CaseGen wiki compile (`wiki_ingest.ingest_document`) currently:

1. Parses the full document text.
2. Stores **verbatim SourceChunks** (L1) for retrieval — full coverage.
3. Runs **one** `wiki_analyze` LLM call on **`text[:14000]` only**.
4. Runs `wiki_write` with analysis JSON + **first 2500 chars** of source.

For long regulations (e.g. SSE trading rules), the Wiki layer only “sees” the document head. Later chapters never enter analysis or write. That violates the product intent of Wiki as a compiled knowledge base.

**User decision:** Coverage-first (option A). Ingest may take longer; content must not be dropped from analysis.

## Goals

1. **Full-document coverage for analyze:** every character of parsed text belongs to at least one analyze window (with intentional overlap at boundaries).
2. **Align with llm_wiki long-source pattern:** semantic/paragraph windows → per-window map analyze → rolling global digest → consolidated analysis → write.
3. **Keep L1 SourceChunk path unchanged** (size ~1200, hybrid retrieve / clause anchor).
4. **Short documents stay single-pass** (no unnecessary multi-call overhead).
5. **No silent partial success** in v1: if any analyze window fails after retries, the ingest job fails.

## Non-goals (v1)

- Disk checkpoint resume (llm_wiki `.llm-wiki/ingest-progress/*`) — optional later; v1 uses in-memory + `IngestJob.step_log_json` progress only.
- Background job queue / cancel API (still synchronous HTTP ingest; may run longer).
- Changing wiki page schema, `[[wikilink]]`, purpose.md, or embedding pipelines.
- Domain-specific retrieve boosts.
- Replacing SourceChunk sizing with analyze window sizing.
- Multi-document cross-wiki entity merge (llm_wiki page-merge across sources).

## Current vs target flow

### Current

```
parse full text
 → replace SourceChunks (full)
 → analyze(text[:14000]) × 1
 → write(analysis + text[:2500])
 → index
```

### Target

```
parse full text
 → replace SourceChunks (full, unchanged)
 → if len(text) ≤ single_pass_budget:
       analysis = wiki_analyze(full text) × 1
   else:
       windows = split_analyze_windows(text)
       digest = ""
       partials = []
       for each window:
           partial = wiki_analyze_window(window, overlap, digest)
           partials.append(partial)
           digest = update_digest(digest, partial)
       analysis = consolidate(partials, digest)
 → write from consolidated analysis
   (source excerpt is auxiliary only; must not be the sole knowledge input)
 → index
```

## Architecture

### New module: `backend/app/services/wiki_long_analyze.py`

Pure functions + orchestration helpers, testable without FastAPI:

| Symbol | Responsibility |
|------------------------|
| `compute_analyze_budget(...)` | Single-pass max chars from config / optional model context |
| `split_analyze_windows(text, target, overlap)` | Full coverage windows |
| `merge_analysis_partials(partials, digest)` | Build final analysis `dict` |
| `run_long_source_analyze(text, chat_fn, prompts, ...)` | Map + digest + consolidate |

`wiki_ingest.ingest_document` calls this instead of inline `ANALYZE_SOURCE_CHARS` slice.

### Unchanged

- `parse_document.parse_file`
- `source_chunking` / `source_chunks_store`
- `wiki_pages_parse.split_wiki_pages` / `pages_from_analysis`
- `wiki_index.rebuild_index`
- Hybrid retrieve / clause index
- Default prompt *types* (`wiki_analyze`, `wiki_write`) — content may gain a **window variant** instruction block (see Prompts)

## Windowing algorithm

Inspired by llm_wiki `splitSourceIntoSemanticChunks` + `semanticBlocks`, adapted to Chinese regulatory plain text (often **no** `#` markdown headings).

### Split priority

1. Blank-line paragraphs (same idea as SourceChunk heading/blank splits).
2. Lines that look like section/clause heads: `第…章/节`, `^\d+\.\d+`, markdown `#` if present.
3. Oversized blocks: split on sentence terminators `。！？；\n` then hard slice only as last resort.
4. Pack blocks into windows of ~`target_chars`.
5. Between consecutive windows, attach `overlap_before` = suffix of previous window main text (~`overlap_chars`, break at paragraph/sentence when possible).

### Coverage invariant

```
windows[0].start == 0
windows[i].end >= windows[i-1].end   # progress
windows[-1].end == len(text)
union of window.main spans covers [0, len(text))
```

Overlap is **extra context** copied into the prompt; main spans must still cover the full document without gaps.

### Sizing (defaults, configurable)

| Parameter | Default | Notes |
|-----------|---------|--------|
| `WIKI_ANALYZE_SINGLE_PASS_CHARS` | `48000` | Below this → one analyze call with full text |
| `WIKI_ANALYZE_WINDOW_CHARS` | `12000` | Target main chars per window (clamp 8k–20k if derived) |
| `WIKI_ANALYZE_WINDOW_OVERLAP` | `1000` | Overlap suffix into next prompt (800–2000) |
| `WIKI_ANALYZE_DIGEST_MAX` | `12000` | Rolling digest cap |
| `WIKI_ANALYZE_PARTIAL_JSON_MAX` | `8000` | Per-window JSON stored/merged cap (trim lists if needed) |
| `WIKI_WRITE_ANALYSIS_CHARS` | `24000` | Max analysis JSON into write prompt |
| Remove | `ANALYZE_SOURCE_CHARS=14000` | Deleted as sole analyze gate |

Rationale vs llm_wiki: their single-pass budget tracks model context up to 300k; CaseGen often hits gateway limits, so defaults are **conservative but multi-window**. Ops can raise via env without code change.

Short-circuit: if after split `len(windows) <= 1`, behave as single-pass full text (even if slightly over budget due to packing).

## Map step (per window)

### Prompt

- **System:** active `wiki_analyze` content, plus a fixed appendix (code or prompt version bump):

  - Analyze **only** the MAIN section for new rules/entities.
  - Use OVERLAP and GLOBAL DIGEST only for continuity; do not re-list everything already in digest unless corrected.
  - Output **JSON only**, same schema as today:
    - `summary_title`, `key_rules`, `api_points`, `test_hints`, `entities`, `suggested_page_types`
  - Additionally (window mode), model should also emit (same JSON object):
    - `window_notes` (optional string): cross-refs / open questions
    - `digest_update` (string): short running summary of the whole doc **after** this window (llm_wiki “Updated Global Digest”)

  If we want zero schema drift for single-pass, single-pass keeps **exact** current schema; window mode uses extended schema only in multi-window path. `parse_json_flexible` already accepts extra keys.

- **User:**

```text
源文件: ...
窗口: i/N
字符范围: start-end
标题提示: ...

# GLOBAL DIGEST
{digest trimmed}

# OVERLAP (previous tail)
{overlap_before}

# MAIN
{window main text}
```

### Execution

- Sequential windows (simpler; matches llm_wiki; easier digest continuity). Parallel map is out of scope for v1.
- Per window: existing `chat_fn` / `chat_completion` with wiki timeout; **2 retries** on empty/LLMError.
- On final failure: raise → job `failed` (no skip).
- `_append_step(job, "wiki_analyze_window", ..., index, total, start, end, chars)`.

### Digest update

```
digest = partial.get("digest_update")
       or heuristic_append(digest, partial key_rules/entities)
digest = trim(digest, WIKI_ANALYZE_DIGEST_MAX)
```

Prefer model `digest_update`; if missing, append top new `key_rules` / `entities` lines deterministically.

## Consolidate step

Build final `analysis: dict` for write + fallback:

```python
{
  "summary_title": first_non_empty(partials.summary_title) or filename stem,
  "key_rules": dedupe_preserve_order(all key_rules),
  "api_points": dedupe_preserve_order(...),
  "test_hints": dedupe_preserve_order(...),
  "entities": dedupe_preserve_order(...),
  "suggested_page_types": union(...),
  "global_digest": digest,
  "window_count": N,
  "coverage": {"chars": len(text), "windows": N},
}
```

Dedupe: normalize whitespace; casefold ascii; keep first occurrence. Cap list lengths to avoid write blow-up (e.g. key_rules ≤ 80, others ≤ 40) **after** merge — caps apply only at consolidate, not by dropping windows.

No extra LLM reduce call in v1 (llm_wiki also concatenates rather than a separate reduce model call for analysis text; we merge structured JSON deterministically).

## Write step changes

1. **Primary input:** consolidated analysis JSON (raised cap `WIKI_WRITE_ANALYSIS_CHARS`, default 24k; if still over, keep `global_digest` + head of each list + note truncated counts).
2. **Source excerpt:** keep a **small** auxiliary excerpt for tone/format only:
   - Prefer **not** only `text[:2500]`.
   - v1 practical choice: `text[:1500]` + if multi-window, also append **middle and tail samples** (e.g. 800 chars from mid, 800 from end) labeled as samples — **or** omit long raw excerpts entirely and rely on analysis lists + `global_digest`.
   - **Recommended:** write user payload = analysis + `global_digest` + index excerpt; **drop raw full-source dependence**. Optional 1–2k head sample only.
3. Existing LLM write → `split_wiki_pages`; on failure → `pages_from_analysis` (extend fallback to surface `global_digest` section if present).
4. Still max 8 pages / doc unless product later raises `MAX_WIKI_PAGES_PER_DOC`.

## Job / observability

`IngestJob.step_log_json` steps:

| step | when |
|------|------|
| `parse` | unchanged |
| `source_chunks` | unchanged |
| `wiki_analyze` | single-pass complete |
| `wiki_analyze_plan` | multi: window count, target, overlap, total chars |
| `wiki_analyze_window` | each window success |
| `wiki_analyze_consolidate` | merged counts |
| `wiki_write` / `wiki_write_fallback` | unchanged meaning |
| `index` | unchanged |
| `error` | unchanged |

Document status semantics unchanged: `ingesting` → `ready` / `failed`.

## Configuration

Env-backed in `app/config.py` (names above). Document in README or config comment only; no UI required in v1.

## Error handling

| Case | Behavior |
|------|----------|
| Window LLM empty / error | retry ×2, then fail job |
| Window JSON parse fail | retry once with “return JSON only” nudge **or** fail (pick: retry raw once, then fail) |
| Consolidate empty lists | fail if **all** lists empty and no digest (nothing to write) |
| Write LLM fail | existing analysis fallback pages |
| Short text | single-pass, no window steps |

## Testing

Unit (no network):

1. `split_analyze_windows` covers full synthetic text; no gaps; overlap present when N>1.
2. Text under budget → one window / single-pass path.
3. `merge_analysis_partials` dedupes and keeps order; sets `window_count`.
4. Ingest integration with **mock chat_fn**:
   - Long text forces ≥2 analyze calls; write sees consolidated rules from **later** window content (assert a marker string only present in tail appears in analysis or pages).
   - Short text → exactly 1 analyze call.
5. Existing ingest tests still pass; update any that assumed 14k truncation.

## Rollout

1. Implement module + wire `wiki_ingest`.
2. Re-ingest (or document that user must re-run ingest) for existing long docs — old Wiki pages stay until re-ingest.
3. Optional follow-ups: checkpoint file, async jobs, raise page limit, parallel windows with locked digest (harder).

## Risks

| Risk | Mitigation |
|------|------------|
| Many LLM calls / slow HTTP | Accept per user; log progress; later async |
| Gateway 502 on 12k window | Lower default window; retries |
| Digest drift / lost detail in write | Keep full merged lists in analysis; digest is navigation aid |
| Prompt schema confusion | Single-pass unchanged schema; extra keys only in window path |
| Duplicate rules across windows | Dedupe on consolidate |

## Success criteria

1. A document longer than single-pass budget produces `wiki_analyze_window` steps covering full `char_count`.
2. A distinctive phrase/rule placed only in the **final 20%** of source appears in consolidated `key_rules` or written wiki body when mock/real analyze extracts it.
3. Documents under budget: still one analyze call; no behavior regression on fixtures.
4. `pytest` green including new long-analyze tests.

## Implementation sketch (files)

| File | Change |
|------|--------|
| `app/config.py` | New env constants; remove reliance on hard 14k in ingest |
| `app/services/wiki_long_analyze.py` | **New** |
| `app/services/wiki_ingest.py` | Call long analyze; fix write payload |
| `app/services/wiki_pages_parse.py` | Optional: fallback include `global_digest` |
| `app/default_prompts/wiki_analyze.md` | Optional short note on window fields (or keep appendix in code) |
| `tests/test_wiki_long_analyze.py` | **New** |
| `tests/test_wiki_ingest*.py` | Adjust mocks |

## Approval

Approved direction from product discussion: **llm_wiki long-source mode for CaseGen analyze (coverage-first)**.

Awaiting sign-off on this written spec before implementation plan.
