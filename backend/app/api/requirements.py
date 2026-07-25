from __future__ import annotations

import json
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models.entities import Requirement
from app.schemas.requirements import RequirementCreate, RequirementOut

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
        title=row.title,
        description=row.description,
        focus_tags=_tags_list(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.post("", response_model=RequirementOut)
def create_requirement(
    body: RequirementCreate,
    session: Session = Depends(get_session),
) -> RequirementOut:
    row = Requirement(
        title=body.title,
        description=body.description,
        focus_tags_json=json.dumps(body.focus_tags or [], ensure_ascii=False),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return to_requirement_out(row)


@router.get("", response_model=List[RequirementOut])
def list_requirements(session: Session = Depends(get_session)) -> list[RequirementOut]:
    rows = session.exec(select(Requirement).order_by(Requirement.id.desc())).all()
    return [to_requirement_out(r) for r in rows]


@router.get("/{requirement_id}", response_model=RequirementOut)
def get_requirement(
    requirement_id: int,
    session: Session = Depends(get_session),
) -> RequirementOut:
    row = session.get(Requirement, requirement_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    return to_requirement_out(row)
