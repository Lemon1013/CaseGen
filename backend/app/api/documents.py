import hashlib
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlmodel import Session, select

from app.config import ALLOWED_EXTENSIONS, MAX_UPLOAD_BYTES, ensure_data_dirs
from app.db import get_session
from app.models.entities import Document
from app.schemas.documents import DocumentOut
from app.services.parse_document import parse_file
from app.services.paths import make_raw_filename, raw_path_for, relative_raw_stored_path

router = APIRouter(prefix="/api/documents", tags=["documents"])


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
        text = parse_file(abs_path)
        char_count = len(text)
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
