"""Authentication bootstrap and cookie-session endpoints."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.models.entities import AuthSession, User
from app.schemas.auth import (
    AuthSessionOut,
    AuthUserOut,
    BootstrapOut,
    LoginBody,
    SetupBody,
)
from app.services.auth import (
    create_initial_user,
    create_session,
    delete_session,
    get_user_for_token,
    hash_password,
    hash_token,
    normalize_username,
    utcnow,
    user_count,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# A fixed, valid scrypt record makes malformed / unknown-user login attempts
# take the same password-verification path without ever exposing account
# existence through timing or error text.
_DUMMY_PASSWORD_HASH = hash_password("casegen-invalid-login-dummy")


def _user_out(user: User) -> AuthUserOut:
    return AuthUserOut(
        id=int(user.id or 0),
        username=user.username,
        display_name=user.display_name or user.username,
        role=user.role,
    )


def _secure_cookie(request: Request) -> bool:
    if config.AUTH_COOKIE_SECURE is not None:
        return config.AUTH_COOKIE_SECURE
    return request.url.scheme == "https"


def _set_session_cookies(response: Response, request: Request, token: str, expires_at) -> None:
    secure = _secure_cookie(request)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    max_age = max(1, int((expires_at - now).total_seconds()))
    cookie_expires = expires_at.replace(tzinfo=timezone.utc)
    response.set_cookie(
        config.AUTH_COOKIE_NAME,
        token,
        max_age=max_age,
        expires=cookie_expires,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    # Double-submit token is intentionally not HttpOnly: the frontend reads it
    # and mirrors it in X-CSRF-Token for unsafe API requests.
    response.set_cookie(
        config.AUTH_CSRF_COOKIE_NAME,
        secrets.token_urlsafe(32),
        max_age=max_age,
        expires=cookie_expires,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(config.AUTH_COOKIE_NAME, path="/")
    response.delete_cookie(config.AUTH_CSRF_COOKIE_NAME, path="/")


@router.get("/bootstrap", response_model=BootstrapOut)
def bootstrap(request: Request, session: Session = Depends(get_session)) -> BootstrapOut:
    user = getattr(request.state, "user", None)
    if user is None:
        token = request.cookies.get(config.AUTH_COOKIE_NAME)
        if token:
            user = get_user_for_token(session, token)
    return BootstrapOut(
        setup_required=user_count(session) == 0,
        user=_user_out(user) if user is not None else None,
    )


@router.post("/setup", response_model=AuthSessionOut)
def setup(
    body: SetupBody,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> AuthSessionOut:
    try:
        user = create_initial_user(
            session,
            username=body.username,
            display_name=body.display_name,
            password=body.password,
        )
    except ValueError as exc:
        code = (
            status.HTTP_409_CONFLICT
            if str(exc) == "Initial setup has already been completed"
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    token, auth_session = create_session(
        session,
        user,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    session.commit()
    _set_session_cookies(response, request, token, auth_session.expires_at)
    return AuthSessionOut(user=_user_out(user), expires_at=auth_session.expires_at)


@router.post("/login", response_model=AuthSessionOut)
def login(
    body: LoginBody,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> AuthSessionOut:
    try:
        normalized = normalize_username(body.username)
    except ValueError:
        normalized = ""
    user = session.exec(select(User).where(User.username == normalized)).first() if normalized else None
    password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    valid = verify_password(body.password, password_hash)
    if user is None or not user.is_active or not valid:
        # Deliberately do not distinguish unknown, inactive, and bad-password
        # cases so clients cannot enumerate local accounts.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Session"},
        )
    token, auth_session = create_session(
        session,
        user,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    session.commit()
    _set_session_cookies(response, request, token, auth_session.expires_at)
    return AuthSessionOut(user=_user_out(user), expires_at=auth_session.expires_at)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, session: Session = Depends(get_session)) -> Response:
    delete_session(session, request.cookies.get(config.AUTH_COOKIE_NAME))
    _clear_session_cookies(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


def _session_user(request: Request, session: Session) -> User:
    user = getattr(request.state, "user", None)
    if user is None:
        user = get_user_for_token(session, request.cookies.get(config.AUTH_COOKIE_NAME))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


@router.get("/session", response_model=AuthSessionOut)
def current_session(request: Request, session: Session = Depends(get_session)) -> AuthSessionOut:
    user = _session_user(request, session)
    token = request.cookies.get(config.AUTH_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    token_row = session.exec(
        select(AuthSession)
        .where(AuthSession.token_hash == hash_token(token))
    ).first()
    if token_row is None or token_row.user_id != user.id or token_row.expires_at <= utcnow():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return AuthSessionOut(user=_user_out(user), expires_at=token_row.expires_at)


@router.get("/me", response_model=AuthUserOut)
def me(request: Request, session: Session = Depends(get_session)) -> AuthUserOut:
    return _user_out(_session_user(request, session))
