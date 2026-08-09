from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, col, func, select

from app.db import get_session
from app.models.entities import PromptTemplate
from app.schemas.prompts import PromptCreate, PromptOut, PromptUpdate

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _next_version(session: Session, prompt_type: str) -> int:
    current = session.exec(
        select(func.max(PromptTemplate.version)).where(PromptTemplate.type == prompt_type)
    ).one()
    if current is None:
        return 1
    return int(current) + 1


@router.get("", response_model=List[PromptOut])
def list_prompts(
    type: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> list[PromptTemplate]:
    stmt = select(PromptTemplate).order_by(col(PromptTemplate.id).desc())
    if type:
        stmt = stmt.where(PromptTemplate.type == type)
    return list(session.exec(stmt).all())


@router.post("", response_model=PromptOut)
def create_prompt(
    body: PromptCreate,
    session: Session = Depends(get_session),
) -> PromptTemplate:
    row = PromptTemplate(
        name=body.name,
        type=body.type,
        content=body.content,
        version=_next_version(session, body.type),
        is_active=body.is_active,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.get("/{prompt_id}", response_model=PromptOut)
def get_prompt(prompt_id: int, session: Session = Depends(get_session)) -> PromptTemplate:
    row = session.get(PromptTemplate, prompt_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return row


@router.put("/{prompt_id}", response_model=PromptOut)
def update_prompt(
    prompt_id: int,
    body: PromptUpdate,
    session: Session = Depends(get_session),
) -> PromptTemplate:
    row = session.get(PromptTemplate, prompt_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Prompt not found")

    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(row, key, value)
    row.updated_at = _utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
