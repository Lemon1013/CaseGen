from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import get_session
from app.models.entities import ModelConfig
from app.schemas.models_cfg import ModelCreate, ModelOut, ModelPingOut, ModelUpdate
from app.services.llm import LLMError, chat_completion

router = APIRouter(prefix="/api/models", tags=["models"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def mask_api_key(api_key: str) -> str:
    if not api_key:
        return "***"
    return "***" + api_key[-4:]


def _clear_default_siblings(session: Session, keep_id: int | None = None) -> None:
    rows = session.exec(
        select(ModelConfig).where(ModelConfig.is_default == True)  # noqa: E712
    ).all()
    for row in rows:
        if keep_id is not None and row.id == keep_id:
            continue
        row.is_default = False
        row.updated_at = _utcnow()
        session.add(row)


def _promote_latest_model(session: Session) -> None:
    replacement = session.exec(
        select(ModelConfig)
        .where(ModelConfig.is_default == False)  # noqa: E712
        .order_by(ModelConfig.id.desc())
    ).first()
    if replacement is not None:
        replacement.is_default = True
        replacement.updated_at = _utcnow()
        session.add(replacement)


def to_model_out(row: ModelConfig) -> ModelOut:
    return ModelOut(
        id=row.id,
        name=row.name,
        base_url=row.base_url,
        api_key=mask_api_key(row.api_key),
        model_name=row.model_name,
        is_default=row.is_default,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.post("", response_model=ModelOut)
def create_model(
    body: ModelCreate,
    session: Session = Depends(get_session),
) -> ModelOut:
    if body.is_default:
        _clear_default_siblings(session)
        session.flush()

    row = ModelConfig(
        name=body.name,
        base_url=body.base_url,
        api_key=body.api_key,
        model_name=body.model_name,
        is_default=body.is_default,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return to_model_out(row)


@router.get("", response_model=List[ModelOut])
def list_models(session: Session = Depends(get_session)) -> list[ModelOut]:
    rows = session.exec(select(ModelConfig).order_by(ModelConfig.id.desc())).all()
    return [to_model_out(r) for r in rows]


@router.get("/{model_id}", response_model=ModelOut)
def get_model(model_id: int, session: Session = Depends(get_session)) -> ModelOut:
    row = session.get(ModelConfig, model_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Model not found")
    return to_model_out(row)


@router.put("/{model_id}", response_model=ModelOut)
def update_model(
    model_id: int,
    body: ModelUpdate,
    session: Session = Depends(get_session),
) -> ModelOut:
    row = session.get(ModelConfig, model_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Model not found")

    data = body.model_dump(exclude_unset=True)
    if data.get("is_default") is True:
        _clear_default_siblings(session, keep_id=row.id)
        session.flush()
    for key, value in data.items():
        setattr(row, key, value)
    row.updated_at = _utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return to_model_out(row)


@router.delete("/{model_id}")
def delete_model(model_id: int, session: Session = Depends(get_session)) -> dict:
    row = session.get(ModelConfig, model_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Model not found")
    was_default = row.is_default
    session.delete(row)
    session.flush()
    if was_default:
        _promote_latest_model(session)
    session.commit()
    return {"ok": True}


@router.post("/{model_id}/ping", response_model=ModelPingOut)
def ping_model(model_id: int, session: Session = Depends(get_session)) -> ModelPingOut:
    row = session.get(ModelConfig, model_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Model not found")
    try:
        content, _usage = chat_completion(
            base_url=row.base_url,
            api_key=row.api_key,
            model=row.model_name,
            messages=[{"role": "user", "content": "ping"}],
            stream=True,
            max_tokens=32,
            max_retries=1,
            thinking=(
                False
                if row.model_name.lower().startswith("deepseek-v4-")
                else None
            ),
        )
    except LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ModelPingOut(ok=True, content=content)
