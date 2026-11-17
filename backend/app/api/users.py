"""Administrator-only local user management endpoints.

The first administrator is still created exclusively through
`/api/auth/setup`; this router only manages accounts created afterwards.
Every route is guarded by the shared permission layer, and no response ever
serializes the `password_hash` column.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import get_session
from app.models.entities import AuthSession, User
from app.schemas.users import UserCreate, UserOut, UserPasswordReset, UserUpdate
from app.services.auth import (
    hash_password,
    normalize_username,
    validate_password,
)
from app.services.permissions import require_admin

router = APIRouter(prefix="/api/users", tags=["users"])


def _out(user: User) -> UserOut:
    return UserOut.model_validate(user)


def _get(session: Session, user_id: int) -> User:
    row = session.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    return row


def _revoke_sessions(session: Session, user_id: int) -> None:
    """Immediately invalidate every session of a disabled/edited account."""

    rows = session.exec(
        select(AuthSession).where(AuthSession.user_id == user_id)
    ).all()
    for row in rows:
        session.delete(row)
    if rows:
        session.flush()


def _ensure_active_admin_survives(
    session: Session, row: User, data: dict
) -> None:
    """Reject a change that would leave the deployment without an admin.

    Without an active administrator /api/users becomes permanently 403 and
    /api/auth/setup refuses to recreate the first account, locking everyone
    out.  Only the transition from "active admin" to something else matters;
    ordinary users and re-enabling/promoting are unaffected.
    """

    demoted = data.get("role") is not None and data.get("role") != "admin"
    losing_admin = (
        row.role == "admin"
        and bool(row.is_active)
        and (data.get("is_active") is False or demoted)
    )
    if not losing_admin:
        return
    other_admins = session.exec(
        select(User.id).where(
            User.id != row.id,
            User.role == "admin",
            User.is_active == True,  # noqa: E712
        )
    ).all()
    if not other_admins:
        raise HTTPException(
            status_code=409,
            detail="At least one active administrator must be retained",
        )


@router.get("", response_model=list[UserOut])
def list_users(request: Request, session: Session = Depends(get_session)) -> list[UserOut]:
    require_admin(request)
    rows = session.exec(select(User).order_by(User.id)).all()
    return [_out(row) for row in rows]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, request: Request, session: Session = Depends(get_session)) -> UserOut:
    require_admin(request)
    try:
        username = normalize_username(body.username)
        password = validate_password(body.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row = User(
        username=username,
        display_name=(body.display_name or "").strip()[:80],
        password_hash=hash_password(password),
        role=body.role,
        is_active=True,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Username already exists") from exc
    session.refresh(row)
    return _out(row)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: UserUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> UserOut:
    require_admin(request)
    row = _get(session, user_id)
    data = body.model_dump(exclude_unset=True)
    _ensure_active_admin_survives(session, row, data)
    if "display_name" in data and data["display_name"] is not None:
        data["display_name"] = data["display_name"].strip()[:80]
    for key, value in data.items():
        if value is not None:
            setattr(row, key, value)
    # A disabled account must lose its live sessions right away; the global
    # authentication middleware also refuses inactive users on the next hop.
    if data.get("is_active") is False:
        _revoke_sessions(session, user_id)
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _out(row)


@router.post("/{user_id}/reset-password", response_model=UserOut)
def reset_user_password(
    user_id: int,
    body: UserPasswordReset,
    request: Request,
    session: Session = Depends(get_session),
) -> UserOut:
    require_admin(request)
    row = _get(session, user_id)
    try:
        password = validate_password(body.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row.password_hash = hash_password(password)
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    # Resetting credentials invalidates any session issued with the old hash.
    _revoke_sessions(session, user_id)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _out(row)
