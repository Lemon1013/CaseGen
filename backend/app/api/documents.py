import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlmodel import Session, col, select

from app import config
from app.config import ALLOWED_EXTENSIONS, MAX_UPLOAD_BYTES, ensure_data_dirs
from app.db import get_session
from app.models.entities import Document, IngestJob, SourceChunk
from app.schemas.documents import DocumentOut, DocumentPreviewOut, RechunkOut, SourceChunkOut
from app.schemas.wiki import IngestJobOut
from app.services.parse_document import parse_document
from app.services.paths import make_raw_filename, raw_path_for, relative_raw_stored_path
from app.services.source_chunks_store import replace_chunks_for_document
from app.services.wiki_ingest import ingest_document
from app.services.wiki_jobs import (
    build_ingest_fingerprint,
    job_matches_fingerprint,
    schedule_ingest_job,
)

router = APIRouter(prefix="/api/documents", tags=["documents"])

# Optional injectable chat_fn for tests: set via monkeypatch on this module attr.
_INGEST_CHAT_FN = None


@router.post("", response_model=DocumentOut)
async def upload_document(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> Document:
    ensure_data_dirs()

    filename = file.filename or "upload.bin"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file extension: {suffix or '(none)'}",
        )

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds max size of {MAX_UPLOAD_BYTES} bytes",
        )

    stored_name = make_raw_filename(filename)
    abs_path = raw_path_for(stored_name)
    abs_path.write_bytes(content)

    digest = hashlib.sha256(content).hexdigest()
    content_type = file.content_type or "application/octet-stream"

    status = "parsed"
    error_message = None
    char_count = 0
    try:
        parsed = parse_document(abs_path)
        char_count = len(parsed.text)
    except Exception as exc:  # parse failures mark document failed
        status = "failed"
        error_message = str(exc)

    doc = Document(
        filename=Path(filename).name,
        stored_path=relative_raw_stored_path(stored_name),
        content_type=content_type,
        sha256=digest,
        status=status,
        char_count=char_count,
        error_message=error_message,
    )
    session.add(doc)
    session.commit()
    session.refresh(doc)
    return doc


@router.get("", response_model=List[DocumentOut])
def list_documents(session: Session = Depends(get_session)) -> list[Document]:
    rows = session.exec(select(Document).order_by(Document.id.desc())).all()
    return list(rows)


@router.get("/{document_id}", response_model=DocumentOut)
def get_document(
    document_id: int,
    session: Session = Depends(get_session),
) -> Document:
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.get("/{document_id}/preview", response_model=DocumentPreviewOut)
def preview_document(
    document_id: int,
    max_chars: int = Query(50000, ge=500, le=200000),
    session: Session = Depends(get_session),
) -> DocumentPreviewOut:
    """Parse the immutable upload on demand for quality display and preview."""
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    stored = Path((doc.stored_path or "").replace("\\", "/"))
    path = stored if stored.is_absolute() else config.DATA_DIR / stored
    try:
        resolved = path.resolve()
        resolved.relative_to(config.DATA_DIR.resolve())
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Document path is outside data directory") from exc
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="Source file not found on disk")
    try:
        parsed = parse_document(resolved)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Parse failed: {exc}") from exc
    diagnostics = parsed.diagnostics
    payload = {
        "is_empty": diagnostics.is_empty,
        "replacement_char_count": diagnostics.replacement_char_count,
        "replacement_char_rate": diagnostics.replacement_char_rate,
        "garbled_char_count": diagnostics.garbled_char_count,
        "garbled_char_rate": diagnostics.garbled_char_rate,
        "suspicious_scanned_pdf": diagnostics.suspicious_scanned_pdf,
        "page_count": diagnostics.page_count,
        "pages_with_text": diagnostics.pages_with_text,
        "warnings": list(diagnostics.warnings),
        "errors": list(diagnostics.errors),
    }
    return DocumentPreviewOut(
        document_id=document_id,
        filename=doc.filename,
        text=parsed.text[:max_chars],
        char_count=len(parsed.text),
        returned_chars=min(len(parsed.text), max_chars),
        truncated=len(parsed.text) > max_chars,
        quality_ok=diagnostics.quality_ok,
        diagnostics=payload,
    )


@router.post("/{document_id}/ingest", response_model=IngestJobOut)
def start_ingest(
    document_id: int,
    session: Session = Depends(get_session),
    force: bool = Query(False, description="Force a new ingest after a previous terminal job"),
) -> IngestJobOut:
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    # The SQLite row is the durable queue.  A second request for the same
    # document returns the existing active job and cannot start concurrent
    # page replacement/LLM work.
    active = session.exec(
        select(IngestJob)
        .where(
            IngestJob.document_id == document_id,
            IngestJob.status.in_(["queued", "running"]),
        )
        .order_by(IngestJob.id.desc())
    ).first()
    if active is not None:
        return active

    fingerprint = build_ingest_fingerprint(session, doc)
    if not force:
        completed = session.exec(
            select(IngestJob)
            .where(
                IngestJob.document_id == document_id,
                IngestJob.status == "success",
            )
            .order_by(IngestJob.id.desc())
        ).first()
        if completed is not None and job_matches_fingerprint(completed, fingerprint):
            return completed

    job = IngestJob(
        document_id=document_id,
        status="queued",
        stage="queued",
        progress=0,
        plan_json=json.dumps({"ingest_fingerprint": fingerprint}, ensure_ascii=False),
        step_log_json="[]",
    )
    doc.status = "ingesting"
    doc.error_message = None
    doc.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(doc)
    session.add(job)
    session.commit()
    session.refresh(job)

    # Injected chat functions are the deterministic compatibility hook used
    # by existing tests.  Production (chat_fn is None) always queues and
    # returns without waiting for an LLM call.
    if _INGEST_CHAT_FN is not None:
        job = ingest_document(
            session,
            document_id,
            chat_fn=_INGEST_CHAT_FN,
            job=job,
        )
    else:
        schedule_ingest_job(job.id)
    return job


@router.get("/{document_id}/chunks", response_model=List[SourceChunkOut])
def list_document_chunks(
    document_id: int,
    session: Session = Depends(get_session),
) -> list[SourceChunk]:
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    rows = session.exec(
        select(SourceChunk)
        .where(SourceChunk.document_id == document_id)
        .order_by(col(SourceChunk.chunk_index).asc())
    ).all()
    return list(rows)


@router.post("/{document_id}/rechunk", response_model=RechunkOut)
def rechunk_document(
    document_id: int,
    session: Session = Depends(get_session),
) -> RechunkOut:
    """Rebuild verbatim source chunks without re-running LLM wiki compile."""
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    stored = (doc.stored_path or "").replace("\\", "/")
    path = Path(stored)
    if not path.is_absolute():
        path = config.DATA_DIR / stored
    if not path.exists():
        raise HTTPException(status_code=404, detail="Source file not found on disk")
    try:
        parsed = parse_document(path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Parse failed: {exc}") from exc
    rows = replace_chunks_for_document(
        session,
        document_id,
        parsed,
        chunk_chars=config.SOURCE_CHUNK_CHARS,
        overlap_chars=config.SOURCE_CHUNK_OVERLAP,
    )
    session.commit()
    return RechunkOut(document_id=document_id, chunk_count=len(rows))
