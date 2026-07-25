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
from app.services.parse_document import parse_file
from app.services.wiki_index import rebuild_index
from app.services.wiki_pages_parse import (
    pages_from_analysis,
    parse_json_flexible,
    split_wiki_pages,
)

MAX_WIKI_PAGES_PER_DOC = 8
INDEX_EXCERPT_CHARS = 4000
# wiki_write only needs analysis; re-sending full source often triggers gateway 502
WRITE_SOURCE_EXCERPT_CHARS = 2500
# Cap analyze input so first LLM call stays reliable on long regulations
ANALYZE_SOURCE_CHARS = 14000

ChatFn = Callable[[list[dict[str, str]]], Any]


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
            max_retries=3,
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
) -> IngestJob:
    """Two-step wiki compile: analyze → write pages → index."""
    doc = session.get(Document, document_id)
    if doc is None:
        raise ValueError(f"Document {document_id} not found")

    job = IngestJob(
        document_id=document_id,
        status="running",
        step_log_json="[]",
    )
    doc.status = "ingesting"
    doc.error_message = None
    doc.updated_at = _utcnow()
    session.add(doc)
    session.add(job)
    session.commit()
    session.refresh(job)
    session.refresh(doc)

    try:
        if chat_fn is None:
            chat_fn = _default_chat_fn(session)

        # 1) Parse source text
        abs_path = _resolve_document_path(doc)
        if not abs_path.exists():
            raise FileNotFoundError(f"Source file not found: {abs_path}")
        text = parse_file(abs_path)
        _append_step(job, "parse", f"Parsed {len(text)} characters", char_count=len(text))
        session.add(job)
        session.commit()

        # 2) Analyze
        analyze_prompt = _load_active_prompt(session, "wiki_analyze")
        analyze_text = text if len(text) <= ANALYZE_SOURCE_CHARS else (
            text[:ANALYZE_SOURCE_CHARS]
            + f"\n\n[... 原文共 {len(text)} 字，已截断至 {ANALYZE_SOURCE_CHARS} 字用于分析 ...]"
        )
        analyze_messages = [
            {"role": "system", "content": analyze_prompt.content},
            {
                "role": "user",
                "content": (
                    f"源文件路径: {doc.stored_path}\n"
                    f"文件名: {doc.filename}\n\n"
                    f"# 原文\n{analyze_text}"
                ),
            },
        ]
        analysis_raw = _call_chat(chat_fn, analyze_messages)
        analysis = parse_json_flexible(analysis_raw)
        _append_step(
            job,
            "wiki_analyze",
            "Analysis JSON parsed",
            keys=list(analysis.keys()),
            source_chars=len(text),
            analyze_chars=len(analyze_text),
        )
        session.add(job)
        session.commit()

        # 3) Write wiki pages (LLM preferred; analysis fallback on gateway failure)
        write_prompt = _load_active_prompt(session, "wiki_write")
        index_excerpt = _read_index_excerpt()
        # Keep payload lean: analysis already carries rules/entities; full dual context
        # has been observed to 502 on long finance/exchange documents.
        analysis_json = json.dumps(analysis, ensure_ascii=False, indent=2)
        if len(analysis_json) > 12000:
            analysis_json = analysis_json[:12000] + "\n...[truncated]"
        write_messages = [
            {"role": "system", "content": write_prompt.content},
            {
                "role": "user",
                "content": (
                    f"# Step A 分析结果\n```json\n{analysis_json}\n```\n\n"
                    f"# 源路径\n{doc.stored_path}\n\n"
                    f"# 现有 index 摘要\n{index_excerpt[:2000]}\n\n"
                    f"# 原文摘录（辅助）\n{text[:WRITE_SOURCE_EXCERPT_CHARS]}"
                ),
            },
        ]
        pages: list[dict] = []
        write_mode = "llm"
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
            f"Split into {len(pages)} page(s) via {write_mode}",
            mode=write_mode,
        )
        session.add(job)
        session.commit()

        # 4) Persist pages (replace previous for this document)
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
        all_rows = list(session.exec(select(WikiPageRow).order_by(WikiPageRow.id)).all())
        rebuild_index(all_rows)
        _append_step(job, "index", f"Index rebuilt with {len(all_rows)} page(s)")

        doc.status = "ready"
        doc.char_count = len(text)
        doc.error_message = None
        doc.updated_at = _utcnow()
        job.status = "success"
        job.error_message = None
        job.updated_at = _utcnow()
        session.add(doc)
        session.add(job)
        session.commit()
        session.refresh(job)
        return job

    except Exception as exc:
        err = str(exc)
        _append_step(job, "error", err)
        job.status = "failed"
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
