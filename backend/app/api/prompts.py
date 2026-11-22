from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, func, select

from app.db import get_session
from app.models.entities import GenerationTask, PromptRevision, PromptTemplate, User
from app.schemas.prompts import PromptCreate, PromptForkRequest, PromptOut, PromptUpdate
from app.services.permissions import Identity, current_user
from app.services.prompt_permissions import (
    can_fork_prompt,
    can_manage_prompt,
    can_view_prompt,
    resolve_prompt_actor,
)
from app.services.prompts_seed import (
    MULTI_ACTIVE_PROMPT_TYPES,
    PROMPT_TYPES,
    PROMPT_VISIBILITIES,
    USER_MANAGEABLE_PROMPT_TYPES,
)

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


def _deactivate_active_prompts(
    session: Session,
    prompt_type: str,
    *,
    exclude_id: int | None = None,
) -> None:
    if prompt_type in MULTI_ACTIVE_PROMPT_TYPES:
        return
    statement = select(PromptTemplate).where(
        PromptTemplate.type == prompt_type,
        PromptTemplate.is_active == True,  # noqa: E712
    )
    if exclude_id is not None:
        statement = statement.where(PromptTemplate.id != exclude_id)
    for row in session.exec(statement).all():
        row.is_active = False
        session.add(row)
    # Flush deactivations before the caller activates another row; SQLite's
    # partial unique index checks each statement, not the final transaction.
    session.flush()


def _load_owner_usernames(session: Session, rows: list[PromptTemplate]) -> dict[int, str]:
    owner_ids = sorted(
        {int(row.owner_user_id) for row in rows if row.owner_user_id is not None}
    )
    if not owner_ids:
        return {}
    pairs = session.exec(
        select(User.id, User.username).where(col(User.id).in_(owner_ids))
    ).all()
    return {int(uid): str(username) for uid, username in pairs if uid is not None}


def _to_prompt_out(
    row: PromptTemplate,
    user: Identity | None,
    owner_usernames: dict[int, str],
) -> PromptOut:
    manageable = can_manage_prompt(row, user)
    forkable = can_fork_prompt(row, user)
    owner_username = (
        owner_usernames.get(int(row.owner_user_id))
        if row.owner_user_id is not None
        else None
    )
    visibility = (row.visibility or "system").strip() or "system"
    return PromptOut(
        id=int(row.id or 0),
        name=row.name,
        type=row.type,
        content=row.content,
        version=int(row.version),
        is_active=bool(row.is_active),
        owner_user_id=row.owner_user_id,
        owner_username=owner_username,
        visibility=visibility,
        can_edit=manageable,
        can_delete=manageable,
        can_fork=forkable,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=List[PromptOut])
def list_prompts(
    request: Request,
    type: Optional[str] = Query(default=None),
    visibility: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> list[PromptOut]:
    if visibility and visibility not in PROMPT_VISIBILITIES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid prompt visibility: {visibility}",
        )
    stmt = (
        select(PromptTemplate)
        .where(col(PromptTemplate.type).in_(PROMPT_TYPES))
        .order_by(col(PromptTemplate.is_active).desc(), col(PromptTemplate.id).desc())
    )
    if type:
        stmt = stmt.where(PromptTemplate.type == type)
    if visibility:
        stmt = stmt.where(PromptTemplate.visibility == visibility)
    user = current_user(request)
    rows = [row for row in session.exec(stmt).all() if can_view_prompt(row, user)]
    owner_usernames = _load_owner_usernames(session, rows)
    return [_to_prompt_out(row, user, owner_usernames) for row in rows]


@router.post("", response_model=PromptOut)
def create_prompt(
    body: PromptCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> PromptOut:
    if body.type not in PROMPT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid prompt type: {body.type}",
        )

    user = current_user(request)
    effective_admin, effective_user_id = resolve_prompt_actor(user)

    if body.type not in USER_MANAGEABLE_PROMPT_TYPES:
        if not effective_admin:
            raise HTTPException(
                status_code=403,
                detail="Only administrators can create system engine prompts",
            )
        if body.visibility not in (None, "system"):
            raise HTTPException(
                status_code=422,
                detail="System engine prompts must use visibility='system'",
            )
        visibility = "system"
        owner_user_id: Optional[int] = None
    else:
        if not effective_admin:
            if effective_user_id is None:
                raise HTTPException(status_code=401, detail="Not authenticated")
            requested_vis = body.visibility or "private"
            if requested_vis == "system":
                raise HTTPException(
                    status_code=403,
                    detail="Only administrators can create system prompts",
                )
            visibility = requested_vis
            owner_user_id = effective_user_id
        else:
            if user is None:
                visibility = "system"
                owner_user_id = None
            else:
                requested_vis = body.visibility or "system"
                if requested_vis == "system":
                    visibility = "system"
                    owner_user_id = None
                else:
                    visibility = requested_vis
                    owner_user_id = effective_user_id

    if body.is_active:
        _deactivate_active_prompts(session, body.type)
    row = PromptTemplate(
        name=body.name,
        type=body.type,
        content=body.content,
        version=_next_version(session, body.type),
        is_active=body.is_active,
        owner_user_id=owner_user_id,
        visibility=visibility,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Another active prompt of this type is already enabled",
        ) from exc
    session.refresh(row)
    owner_usernames = _load_owner_usernames(session, [row])
    return _to_prompt_out(row, user, owner_usernames)


@router.get("/{prompt_id}", response_model=PromptOut)
def get_prompt(
    prompt_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> PromptOut:
    row = session.get(PromptTemplate, prompt_id)
    user = current_user(request)
    if row is None or row.type not in PROMPT_TYPES or not can_view_prompt(row, user):
        raise HTTPException(status_code=404, detail="Prompt not found")
    owner_usernames = _load_owner_usernames(session, [row])
    return _to_prompt_out(row, user, owner_usernames)


@router.put("/{prompt_id}", response_model=PromptOut)
def update_prompt(
    prompt_id: int,
    body: PromptUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> PromptOut:
    row = session.get(PromptTemplate, prompt_id)
    user = current_user(request)
    if row is None or row.type not in PROMPT_TYPES or not can_view_prompt(row, user):
        raise HTTPException(status_code=404, detail="Prompt not found")
    if not can_manage_prompt(row, user):
        raise HTTPException(status_code=403, detail="No permission to modify this prompt")

    effective_admin, effective_user_id = resolve_prompt_actor(user)
    data = body.model_dump(exclude_unset=True, exclude_none=True)
    current_visibility = (row.visibility or "system").strip() or "system"

    new_owner_user_id = row.owner_user_id
    if "visibility" in data:
        target_visibility = data["visibility"]
        if row.type not in USER_MANAGEABLE_PROMPT_TYPES:
            if target_visibility != "system":
                raise HTTPException(
                    status_code=422,
                    detail="System engine prompts must keep visibility='system'",
                )
        else:
            is_owner = (
                effective_user_id is not None
                and row.owner_user_id is not None
                and int(row.owner_user_id) == effective_user_id
            )
            if not effective_admin:
                if target_visibility == "system":
                    raise HTTPException(
                        status_code=403,
                        detail="Only administrators can set prompt visibility to system",
                    )
            else:
                if (
                    current_visibility == "shared"
                    and row.owner_user_id is not None
                    and not is_owner
                ):
                    if target_visibility == "private":
                        raise HTTPException(
                            status_code=403,
                            detail="Administrators cannot change another user's shared prompt to private",
                        )
                    if target_visibility == "system":
                        raise HTTPException(
                            status_code=403,
                            detail="Administrators cannot change another user's shared prompt to system",
                        )
                if target_visibility == "system":
                    new_owner_user_id = None
                elif (
                    target_visibility in {"private", "shared"}
                    and row.owner_user_id is None
                ):
                    if user is None:
                        data["visibility"] = "system"
                        new_owner_user_id = None
                    else:
                        new_owner_user_id = effective_user_id

    next_visibility = data.get("visibility", current_visibility)
    next_is_active = data.get("is_active", row.is_active)
    if (
        row.type in USER_MANAGEABLE_PROMPT_TYPES
        and current_visibility == "system"
        and row.is_active
        and (next_visibility != "system" or not next_is_active)
    ):
        replacement = session.exec(
            select(PromptTemplate).where(
                PromptTemplate.type == row.type,
                PromptTemplate.visibility == "system",
                PromptTemplate.is_active == True,  # noqa: E712
                PromptTemplate.id != prompt_id,
            )
        ).first()
        if replacement is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot deactivate or change visibility of the last active "
                    "system generate prompt; activate another system prompt first"
                ),
            )

    row.owner_user_id = new_owner_user_id
    if data.get("is_active") is True:
        _deactivate_active_prompts(session, row.type, exclude_id=row.id)
    for key, value in data.items():
        setattr(row, key, value)
    row.updated_at = _utcnow()
    session.add(row)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Another active prompt of this type is already enabled",
        ) from exc
    session.refresh(row)
    owner_usernames = _load_owner_usernames(session, [row])
    return _to_prompt_out(row, user, owner_usernames)


@router.delete("/{prompt_id}")
def delete_prompt(
    prompt_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, int | bool]:
    """Delete an unused prompt without breaking active pipelines or history."""

    row = session.get(PromptTemplate, prompt_id)
    user = current_user(request)
    if row is None or row.type not in PROMPT_TYPES or not can_view_prompt(row, user):
        raise HTTPException(status_code=404, detail="Prompt not found")
    if not can_manage_prompt(row, user):
        raise HTTPException(status_code=403, detail="No permission to delete this prompt")

    task = session.exec(
        select(GenerationTask).where(
            GenerationTask.prompt_template_id == prompt_id
        )
    ).first()
    if task is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Prompt is referenced by task #{task.id}",
        )
    revision = session.exec(
        select(PromptRevision).where(PromptRevision.base_prompt_id == prompt_id)
    ).first()
    if revision is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Prompt is referenced by revision #{revision.id}",
        )

    row_visibility = (row.visibility or "system").strip() or "system"
    requires_active_fallback = (
        row.type not in USER_MANAGEABLE_PROMPT_TYPES or row_visibility == "system"
    )
    if row.is_active and requires_active_fallback:
        replacement_stmt = select(PromptTemplate).where(
            PromptTemplate.type == row.type,
            PromptTemplate.is_active == True,  # noqa: E712
            PromptTemplate.id != prompt_id,
        )
        if row.type in USER_MANAGEABLE_PROMPT_TYPES:
            replacement_stmt = replacement_stmt.where(
                PromptTemplate.visibility == "system"
            )
        replacement = session.exec(replacement_stmt).first()
        if replacement is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot delete the last active prompt of this type; "
                    "activate another prompt first"
                ),
            )

    session.delete(row)
    session.commit()
    return {"ok": True, "id": prompt_id}


@router.post("/{prompt_id}/fork", response_model=PromptOut)
def fork_prompt(
    prompt_id: int,
    request: Request,
    body: Optional[PromptForkRequest] = None,
    session: Session = Depends(get_session),
) -> PromptOut:
    """Create a user-owned copy of a visible generate prompt template."""

    row = session.get(PromptTemplate, prompt_id)
    user = current_user(request)
    if row is None or row.type not in PROMPT_TYPES or not can_view_prompt(row, user):
        raise HTTPException(status_code=404, detail="Prompt not found")
    if row.type not in USER_MANAGEABLE_PROMPT_TYPES:
        raise HTTPException(
            status_code=422,
            detail="Only generate prompts can be forked",
        )

    payload = body if body is not None else PromptForkRequest()
    name = (payload.name or "").strip() or f"{row.name} (副本)"
    owner_user_id = int(user.id) if user is not None and user.id is not None else None
    visibility = payload.visibility if user is not None else "system"

    forked = PromptTemplate(
        name=name,
        type="generate",
        content=row.content,
        version=_next_version(session, "generate"),
        is_active=True,
        owner_user_id=owner_user_id,
        visibility=visibility,
    )
    session.add(forked)
    session.commit()
    session.refresh(forked)
    owner_usernames = _load_owner_usernames(session, [forked])
    return _to_prompt_out(forked, user, owner_usernames)
