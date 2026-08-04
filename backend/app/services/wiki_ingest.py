from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from sqlmodel import Session, select

from app import config
from app.models.entities import Document, IngestJob, ModelConfig, PromptTemplate, WikiPageRow
from app.services.llm import LLMError, chat_completion
from app.services.parse_document import parse_document
from app.services.wiki_index import rebuild_index
from app.services.wiki_log import log_ingest
from app.services.wiki_overview import rebuild_overview
from app.services.source_chunks_store import replace_chunks_for_document
from app.services.wiki_apply import apply_wiki_plan
from app.services.wiki_candidates import recall_wiki_candidates_from_session
from app.services.wiki_long_analyze import run_long_source_analyze
from app.services.wiki_pages_parse import (
    parse_wiki_write_output,
    pages_from_analysis,
    split_wiki_pages,
)

MAX_WIKI_PAGES_PER_DOC = 8
INDEX_EXCERPT_CHARS = 4000

ChatFn = Callable[[list[dict[str, str]]], Any]


class IngestCancelled(Exception):
    """Internal control flow used when a cancellation is observed.

    Cancellation is checked between pipeline stages and after each long-source
    analysis window.  A single in-flight LLM HTTP request cannot be
    interrupted by the SQLite flag; it is observed at the next boundary.
    """


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _slugify(title: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", title.strip(), flags=re.UNICODE)
    s = re.sub(r"-{2,}", "-", s).strip("-").lower()
    return (s[:60] if s else "page") or "page"


def _append_step(job: IngestJob, step: str, message: str, **extra: Any) -> None:
    try:
        log = json.loads(job.step_log_json or "[]")
    except json.JSONDecodeError:
        log = []
    if not isinstance(log, list):
        log = []
    entry = {"step": step, "message": message, "at": _utcnow().isoformat()}
    entry.update(extra)
    log.append(entry)
    job.step_log_json = json.dumps(log, ensure_ascii=False)
    job.updated_at = _utcnow()


def _set_stage(
    session: Session,
    job: IngestJob,
    stage: str,
    progress: int,
    message: Optional[str] = None,
    **extra: Any,
) -> None:
    job.stage = stage
    job.progress = max(0, min(100, int(progress)))
    if message:
        _append_step(job, stage, message, progress=job.progress, **extra)
    job.updated_at = _utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)


def _raise_if_cancelled(
    session: Session,
    job: IngestJob,
    doc: Document,
) -> None:
    """Stop at a durable boundary when the API has requested cancellation."""

    session.refresh(job)
    if not job.cancel_requested and job.status != "cancelled":
        return
    if job.status != "cancelled":
        _append_step(
            job,
            "cancelled",
            "Cancellation requested; stopped at a pipeline boundary",
        )
        job.status = "cancelled"
        job.stage = "cancelled"
        job.error_message = "Ingest cancelled"
        job.updated_at = _utcnow()
        doc.status = "cancelled"
        doc.error_message = None
        doc.updated_at = _utcnow()
        session.add(doc)
        session.add(job)
        session.commit()
    raise IngestCancelled()


def _resolve_document_path(doc: Document) -> Path:
    stored = (doc.stored_path or "").replace("\\", "/")
    path = Path(stored)
    if path.is_absolute():
        return path
    return config.DATA_DIR / stored


def _load_active_prompt(session: Session, prompt_type: str) -> PromptTemplate:
    row = session.exec(
        select(PromptTemplate).where(
            PromptTemplate.type == prompt_type,
            PromptTemplate.is_active == True,  # noqa: E712
        )
    ).first()
    if row is None:
        raise ValueError(f"No active prompt for type={prompt_type}")
    return row


def _call_chat(chat_fn: ChatFn, messages: list[dict[str, str]]) -> str:
    result = chat_fn(messages)
    if isinstance(result, tuple):
        content = result[0]
    else:
        content = result
    if not content or not str(content).strip():
        raise LLMError("Empty LLM content")
    return str(content)


def _default_chat_fn(session: Session) -> ChatFn:
    model = session.exec(
        select(ModelConfig).where(ModelConfig.is_default == True)  # noqa: E712
    ).first()
    if model is None:
        model = session.exec(select(ModelConfig).order_by(ModelConfig.id.desc())).first()
    if model is None:
        raise ValueError("No ModelConfig available for LLM ingest")

    def _chat(messages: list[dict[str, str]]) -> str:
        content, _usage = chat_completion(
            base_url=model.base_url,
            api_key=model.api_key,
            model=model.model_name,
            messages=messages,
            timeout=float(config.LLM_WIKI_TIMEOUT_SEC),
            # Analyze windows already own their retry policy.  Retrying again
            # inside each HTTP call multiplies attempts and can make a queued
            # job appear stuck for many minutes.  Wiki write falls back to a
            # deterministic page builder on failure.
            max_retries=0,
        )
        return content

    return _chat


def _read_index_excerpt() -> str:
    index_path = config.WIKI_DIR / "index.md"
    if not index_path.exists():
        return "# Wiki Index\n"
    text = index_path.read_text(encoding="utf-8", errors="replace")
    return text[:INDEX_EXCERPT_CHARS]


def _delete_existing_pages(session: Session, document_id: int) -> None:
    rows = session.exec(
        select(WikiPageRow).where(WikiPageRow.source_document_id == document_id)
    ).all()
    for row in rows:
        path = Path(row.path or "")
        if not path.is_absolute():
            candidate = config.WIKI_DIR / row.path
            if not candidate.exists():
                candidate = config.WIKI_PAGES_DIR / Path(row.path).name
            path = candidate
        if path.exists() and path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
        session.delete(row)
    if rows:
        session.commit()


def _write_page_file(row: WikiPageRow, body: str, sources: list[str], tags: list[str]) -> Path:
    config.ensure_data_dirs()
    slug = _slugify(row.title)
    filename = f"{slug}-{row.id}.md"
    rel_path = f"pages/{filename}"
    abs_path = config.WIKI_DIR / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    front = {
        "title": row.title,
        "type": row.page_type,
        "sources": sources,
        "tags": tags,
        "updated_at": _utcnow().date().isoformat(),
    }
    fm = yaml_dump_frontmatter(front)
    content = f"{fm}\n{body.strip()}\n"
    abs_path.write_text(content, encoding="utf-8")
    row.path = rel_path
    return abs_path


def yaml_dump_frontmatter(data: dict[str, Any]) -> str:
    import yaml

    dumped = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{dumped}\n---"


def ingest_document(
    session: Session,
    document_id: int,
    chat_fn: Optional[ChatFn] = None,
    *,
    job: Optional[IngestJob] = None,
) -> IngestJob:
    """Two-step wiki compile, optionally using a pre-created persistent Job."""
    doc = session.get(Document, document_id)
    if doc is None:
        raise ValueError(f"Document {document_id} not found")

    if job is None:
        job = IngestJob(
            document_id=document_id,
            status="running",
            stage="parsing",
            progress=5,
            step_log_json="[]",
        )
        session.add(job)
    elif job.document_id != document_id:
        raise ValueError(f"Job {job.id} does not belong to document {document_id}")
    job.status = "running"
    job.stage = "parsing"
    job.progress = max(5, int(job.progress or 0))
    job.error_message = None
    job.updated_at = _utcnow()
    doc.status = "ingesting"
    doc.error_message = None
    doc.updated_at = _utcnow()
    session.add(doc)
    session.commit()
    session.refresh(job)
    session.refresh(doc)

    try:
        _raise_if_cancelled(session, job, doc)
        if chat_fn is None:
            model = session.exec(
                select(ModelConfig).where(ModelConfig.is_default == True)  # noqa: E712
            ).first()
            if model is None:
                model = session.exec(select(ModelConfig).order_by(ModelConfig.id.desc())).first()
            if model is not None:
                job.model_ref = f"id:{model.id}:v{model.model_name}"
        else:
            job.model_ref = job.model_ref or "injected"
        session.add(job)
        session.commit()
        if chat_fn is None:
            chat_fn = _default_chat_fn(session)

        # 1) Parse source text
        abs_path = _resolve_document_path(doc)
        if not abs_path.exists():
            raise FileNotFoundError(f"Source file not found: {abs_path}")
        _set_stage(session, job, "parsing", 10)
        parsed = parse_document(abs_path)
        if not parsed.diagnostics.quality_ok:
            detail = "; ".join(parsed.diagnostics.messages)
            raise ValueError(f"解析质量不足，无法摄入：{detail}")
        text = parsed.text
        _append_step(job, "parse", f"Parsed {len(text)} characters", char_count=len(text))
        job.progress = 15
        session.add(job)
        session.commit()
        _raise_if_cancelled(session, job, doc)

        # 1b) Verbatim source chunks (lossless layer for hybrid retrieve)
        _set_stage(session, job, "chunking", 20)
        chunk_rows = replace_chunks_for_document(
            session,
            document_id,
            parsed,
            chunk_chars=config.SOURCE_CHUNK_CHARS,
            overlap_chars=config.SOURCE_CHUNK_OVERLAP,
        )
        job.progress = 25
        _append_step(
            job,
            "source_chunks",
            f"Stored {len(chunk_rows)} verbatim source chunk(s)",
            chunk_count=len(chunk_rows),
        )
        session.add(job)
        session.commit()
        _raise_if_cancelled(session, job, doc)

        # 2) Analyze (single-pass or multi-window long-source)
        _set_stage(session, job, "analyzing", 30)
        candidate_pages = recall_wiki_candidates_from_session(
            session,
            text=text,
            filename=doc.filename or "",
            top_k=24,
        )
        source_anchor_windows: list[dict[str, Any]] = []
        for row in chunk_rows:
            try:
                clauses = json.loads(row.clause_ids_json or "[]")
            except (TypeError, json.JSONDecodeError):
                clauses = []
            chunk_id = int(row.id) if row.id is not None else None
            source_anchor_windows.append(
                {
                    "document_id": document_id,
                    "source_path": doc.stored_path or "",
                    "chunk_id": chunk_id,
                    "chunk_ids": [chunk_id] if chunk_id is not None else [],
                    "start_char": row.start_char,
                    "end_char": row.end_char,
                    "page_start": row.page_start,
                    "page_end": row.page_end,
                    "section": row.section or None,
                    "clause_ids": clauses if isinstance(clauses, list) else [],
                }
            )
        analyze_prompt = _load_active_prompt(session, "wiki_analyze")
        job.prompt_version_ref = (
            f"wiki_analyze:id:{analyze_prompt.id}:v{analyze_prompt.version}"
        )
        session.add(job)
        session.commit()

        def _on_analyze_step(step: str, message: str, **extra: Any) -> None:
            _append_step(job, step, message, **extra)
            if step == "wiki_analyze_window" and extra.get("total"):
                total = max(1, int(extra["total"]))
                index = min(total, int(extra.get("index") or 0))
                job.stage = "analyzing"
                job.progress = 30 + int(35 * index / total)
            elif step == "wiki_analyze_consolidate":
                job.stage = "planning"
                job.progress = 68
            else:
                job.stage = "analyzing"
                job.progress = max(job.progress, 30)
            job.updated_at = _utcnow()
            session.add(job)
            session.commit()
            _raise_if_cancelled(session, job, doc)

        analyze_result = run_long_source_analyze(
            text,
            chat_fn=chat_fn,
            analyze_system_prompt=analyze_prompt.content,
            source_path=doc.stored_path or "",
            filename=doc.filename or "",
            on_step=_on_analyze_step,
            candidate_pages=candidate_pages,
            existing_page_keys=[
                str(item["page_key"])
                for item in candidate_pages
                if item.get("page_key")
            ],
            source_windows=source_anchor_windows,
        )
        analysis = analyze_result["analysis"]
        try:
            persisted_plan = json.loads(job.plan_json or "{}")
        except json.JSONDecodeError:
            persisted_plan = {}
        if not isinstance(persisted_plan, dict):
            persisted_plan = {}
        persisted_plan.update(
            {
                "candidate_page_keys": [
                    str(item["page_key"])
                    for item in candidate_pages
                    if item.get("page_key")
                ],
                "step_a_plan": analyze_result.get("step_a_plan") or {},
                "window_results": analyze_result.get("window_results") or [],
            }
        )
        job.plan_json = json.dumps(persisted_plan, ensure_ascii=False)
        _set_stage(session, job, "planning", 70)
        _raise_if_cancelled(session, job, doc)
        session.add(job)
        session.commit()

        # 3) Write wiki pages (LLM preferred; analysis fallback on gateway failure)
        write_prompt = _load_active_prompt(session, "wiki_write")
        job.prompt_version_ref = (
            f"{job.prompt_version_ref or ''};wiki_write:id:{write_prompt.id}:v{write_prompt.version}"
        ).strip(";")
        _set_stage(session, job, "writing", 75)
        _raise_if_cancelled(session, job, doc)
        index_excerpt = _read_index_excerpt()
        # Primary knowledge is consolidated analysis (+ digest); keep only a small
        # raw sample for tone/format so long dual-context payloads do not 502.
        analysis_json = json.dumps(analysis, ensure_ascii=False, indent=2)
        write_cap = int(getattr(config, "WIKI_WRITE_ANALYSIS_CHARS", 24000))
        if len(analysis_json) > write_cap:
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
        pages: list[dict] = []
        strict_apply_result = None
        strict_plan = analyze_result.get("step_a_plan") or {}
        strict_operations = (
            strict_plan.get("page_operations")
            if isinstance(strict_plan, dict)
            else None
        ) or []
        write_mode = "llm"
        has_strict_plan = (
            isinstance(strict_plan, dict) and "page_operations" in strict_plan
        )
        if has_strict_plan:
            candidates: list[dict[str, Any]] = []
            if strict_operations:
                try:
                    write_raw = _call_chat(chat_fn, write_messages)
                    candidates = parse_wiki_write_output(
                        write_raw,
                        max_pages=MAX_WIKI_PAGES_PER_DOC,
                        allow_legacy_markdown=False,
                    )
                except (LLMError, ValueError) as write_exc:
                    write_mode = "deterministic_fallback"
                    _append_step(
                        job,
                        "wiki_write_fallback",
                        f"Structured write failed ({write_exc}); using deterministic candidates",
                    )
            else:
                # Step A found no safe page mutation. The apply layer still
                # maintains the source summary, so a Step B model call would
                # be wasteful and its output could only violate the plan.
                write_mode = "plan_noop"
            strict_apply_result = apply_wiki_plan(
                session,
                strict_plan,
                candidates,
                document_id=document_id,
                job_id=job.id,
                max_pages=MAX_WIKI_PAGES_PER_DOC,
            )
            _append_step(
                job,
                "wiki_apply",
                f"Applied {len(strict_apply_result.applied_page_keys)} page(s); "
                f"queued {len(strict_apply_result.review_item_ids)} review item(s)",
                applied_page_keys=strict_apply_result.applied_page_keys,
                noop_page_keys=strict_apply_result.noop_page_keys,
                review_item_ids=strict_apply_result.review_item_ids,
                source_summary_key=strict_apply_result.source_summary_key,
            )
        else:
            try:
                write_raw = _call_chat(chat_fn, write_messages)
                pages = split_wiki_pages(write_raw)
                if not pages:
                    raise ValueError("wiki_write produced no pages")
            except (LLMError, ValueError) as write_exc:
                write_mode = "analysis_fallback"
                pages = pages_from_analysis(
                    analysis,
                    source_path=doc.stored_path or "",
                    filename=doc.filename or "",
                )
                if not pages:
                    raise write_exc
                _append_step(
                    job,
                    "wiki_write_fallback",
                    f"LLM write failed ({write_exc}); built {len(pages)} page(s) from analysis",
                )
            pages = pages[:MAX_WIKI_PAGES_PER_DOC]
        _append_step(
            job,
            "wiki_write",
            f"Prepared {len(pages) if strict_apply_result is None else len(strict_apply_result.applied_page_keys)} page(s) via {write_mode}",
            mode=write_mode,
        )
        session.add(job)
        session.commit()
        _raise_if_cancelled(session, job, doc)

        # 4) Persist pages (replace previous for this document)
        _set_stage(session, job, "writing", 82)
        if strict_apply_result is None:
            _delete_existing_pages(session, document_id)
            written_rows: list[WikiPageRow] = []
            for page in pages:
                tags = page.get("tags") or []
                sources = page.get("sources") or [doc.stored_path]
                if not sources:
                    sources = [doc.stored_path]
                row = WikiPageRow(
                    path="",  # filled after id known
                    title=page["title"],
                    page_type=page.get("page_type") or page.get("type") or "business",
                    source_document_id=document_id,
                    tags_json=json.dumps(tags, ensure_ascii=False),
                )
                session.add(row)
                session.flush()
                _write_page_file(row, page.get("body") or "", sources, tags)
                session.add(row)
                written_rows.append(row)
            session.commit()
            for row in written_rows:
                session.refresh(row)

        # 5) Rebuild full index from all wiki pages
        _set_stage(session, job, "indexing", 95)
        _raise_if_cancelled(session, job, doc)
        all_rows = list(session.exec(select(WikiPageRow).order_by(WikiPageRow.id)).all())
        rebuild_index(all_rows, session=session)
        rebuild_overview(all_rows, session=session)
        _append_step(job, "index", f"Index rebuilt with {len(all_rows)} page(s)")

        doc.status = "ready"
        doc.char_count = len(text)
        doc.error_message = None
        doc.updated_at = _utcnow()
        job.status = "success"
        job.stage = "ready"
        job.progress = 100
        job.error_message = None
        job.updated_at = _utcnow()
        session.add(doc)
        session.add(job)
        session.commit()
        try:
            log_ingest(
                {
                    "job_id": job.id,
                    "document_id": document_id,
                    "status": "success",
                    "stage": job.stage,
                    "applied_pages": (
                        strict_apply_result.applied_page_keys
                        if strict_apply_result is not None
                        else [row.page_key or row.title for row in written_rows]
                    ),
                    "review_item_ids": (
                        strict_apply_result.review_item_ids
                        if strict_apply_result is not None
                        else []
                    ),
                }
            )
        except OSError as log_exc:
            _append_step(job, "log_warning", f"Wiki log append failed: {log_exc}")
            session.add(job)
            session.commit()
        session.refresh(job)
        return job

    except IngestCancelled:
        session.refresh(job)
        return job
    except Exception as exc:
        err = str(exc)
        _append_step(job, "error", err)
        job.status = "failed"
        job.stage = "failed"
        job.error_message = err
        job.updated_at = _utcnow()
        doc.status = "failed"
        doc.error_message = err
        doc.updated_at = _utcnow()
        session.add(doc)
        session.add(job)
        session.commit()
        session.refresh(job)
        return job
