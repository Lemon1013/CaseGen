from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlmodel import Session, select

from app import config
from app.config import ensure_data_dirs
from app.db import get_session
from app.models.entities import Project, Requirement
from app.schemas.requirements import (
    RequirementCreate,
    RequirementDocImportOut,
    RequirementOut,
)
from app.services.parse_document import parse_document
from app.services.paths import (
    make_raw_filename,
    raw_path_for,
    relative_raw_stored_path,
)

router = APIRouter(prefix="/api/requirements", tags=["requirements"])


def _tags_list(row: Requirement) -> list[str]:
    try:
        tags = json.loads(row.focus_tags_json or "[]")
    except json.JSONDecodeError:
        tags = []
    if not isinstance(tags, list):
        return []
    return [str(t) for t in tags]


def to_requirement_out(row: Requirement) -> RequirementOut:
    return RequirementOut(
        id=row.id,
        project_id=row.project_id,
        title=row.title,
        description=row.description,
        focus_tags=_tags_list(row),
        source_filename=row.source_filename,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.post("", response_model=RequirementOut)
def create_requirement(
    body: RequirementCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> RequirementOut:
    from app.services.projects import request_project_id, require_request_project
    current_project_id = request_project_id(request)
    target_project_id = body.project_id or current_project_id
    if target_project_id is not None:
        require_request_project(request, target_project_id)
        project = session.get(Project, target_project_id)
        if project is None or project.status != "active":
            raise HTTPException(status_code=422, detail="Project not found or inactive")
    row = Requirement(
        project_id=target_project_id,
        title=body.title,
        description=body.description,
        focus_tags_json=json.dumps(body.focus_tags or [], ensure_ascii=False),
        source_filename=body.source_filename or None,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return to_requirement_out(row)


@router.post("/import-doc", response_model=RequirementDocImportOut)
async def import_requirement_doc(
    file: UploadFile = File(...),
    project_id: int | None = Query(default=None, ge=1),
) -> RequirementDocImportOut:
    """Parse an uploaded requirement document for the workbench form.

    The raw file is archived under ``data/raw/sources/`` exactly like document
    uploads, but no Document row is created and Wiki ingestion is never
    triggered: a requirement document is user input, not knowledge.
    """
    ensure_data_dirs()
    filename = file.filename or "upload.bin"
    suffix = Path(filename).suffix.lower()
    if suffix not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file extension: {suffix or '(none)'}",
        )

    content = await file.read()
    if len(content) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds max size of {config.MAX_UPLOAD_BYTES} bytes",
        )

    stored_name = make_raw_filename(filename)
    abs_path = raw_path_for(stored_name)
    abs_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()

    try:
        parsed = parse_document(abs_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Parse failed: {exc}") from exc
    text = (parsed.text or "").strip()
    if len(text) < 20:
        raise HTTPException(
            status_code=422,
            detail="Parse failed: parsed document has no extractable text",
        )

    title = (Path(filename).stem or "").strip()[:120]
    return RequirementDocImportOut(
        title=title,
        text=text,
        char_count=len(text),
        stored_path=relative_raw_stored_path(stored_name),
        sha256=digest,
    )


@router.get("", response_model=List[RequirementOut])
def list_requirements(project_id: int | None = None, session: Session = Depends(get_session)) -> list[RequirementOut]:
    statement = select(Requirement)
    if project_id is not None:
        statement = statement.where(Requirement.project_id == project_id)
    rows = session.exec(statement.order_by(Requirement.id.desc())).all()
    return [to_requirement_out(r) for r in rows]


@router.get("/{requirement_id}", response_model=RequirementOut)
def get_requirement(
    requirement_id: int,
    project_id: int | None = None,
    session: Session = Depends(get_session),
) -> RequirementOut:
    row = session.get(Requirement, requirement_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    if project_id is not None and row.project_id != project_id:
        raise HTTPException(status_code=404, detail="Requirement not found in project")
    return to_requirement_out(row)
