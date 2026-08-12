"""Local password and cookie-session primitives for the trusted CaseGen UI."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlmodel import Session, col, select

from app import config
from app.models.entities import AuthSession, User

USERNAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{2,31}$")
MIN_PASSWORD_LENGTH = 10
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SETUP_LOCK = threading.Lock()


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_username(username: str) -> str:
    value = (username or "").strip().casefold()
    if not USERNAME_RE.fullmatch(value):
        raise ValueError(
            "Username must be 3-32 characters and use lowercase letters, digits, '.', '_' or '-'; "
            "it must start with a letter"
        )
    return value


def validate_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > 1024:
        raise ValueError("Password is too long")
    return password


def hash_password(password: str) -> str:
    password = validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return (
        f"scrypt$n={_SCRYPT_N}$r={_SCRYPT_R}$p={_SCRYPT_P}"
        f"$salt={salt.hex()}$hash={digest.hex()}"
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        parts = dict(item.split("=", 1) for item in encoded.split("$")[1:])
        n = int(parts["n"])
        r = int(parts["r"])
        p = int(parts["p"])
        salt = bytes.fromhex(parts["salt"])
        expected = bytes.fromhex(parts["hash"])
        if not (n >= 2**10 and r > 0 and p > 0 and len(expected) >= 16):
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (KeyError, TypeError, ValueError, UnicodeError):
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def cleanup_expired_sessions(session: Session, now: Optional[datetime] = None) -> int:
    now = now or utcnow()
    rows = session.exec(select(AuthSession).where(AuthSession.expires_at <= now)).all()
    for row in rows:
        session.delete(row)
    if rows:
        session.flush()
    return len(rows)


def user_count(session: Session) -> int:
    return len(session.exec(select(User.id)).all())


def create_initial_user(
    session: Session,
    *,
    username: str,
    display_name: str = "",
    password: str,
) -> User:
    """Create the first local account, refusing setup after initialization.

    The process lock protects the common single-process deployment while the
    immediate SQLite transaction serializes concurrent workers sharing the
    same database.  No caller receives a partially-created account.
    """

    normalized = normalize_username(username)
    password = validate_password(password)
    with _SETUP_LOCK:
        # Session has not issued a query at this point in the setup route.  An
        # explicit IMMEDIATE transaction closes the count-then-insert race for
        # separate processes using the same SQLite database.
        session.exec(text("BEGIN IMMEDIATE"))
        try:
            if user_count(session) != 0:
                raise ValueError("Initial setup has already been completed")
            row = User(
                username=normalized,
                display_name=(display_name or "").strip()[:80],
                password_hash=hash_password(password),
                role="admin",
                is_active=True,
            )
            session.add(row)
            session.flush()
            session.commit()
            session.refresh(row)
            return row
        except Exception:
            session.rollback()
            raise


def create_session(
    session: Session,
    user: User,
    *,
    user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[str, AuthSession]:
    now = utcnow()
    cleanup_expired_sessions(session, now)
    existing = session.exec(
        select(AuthSession)
        .where(AuthSession.user_id == user.id)
        .order_by(col(AuthSession.created_at).desc())
    ).all()
    # Keep a small bounded set for multi-tab / operator use.  The newest
    # session is always retained; only stale rows beyond the configured cap
    # are removed.
    for row in existing[max(0, config.AUTH_MAX_SESSIONS_PER_USER - 1) :]:
        session.delete(row)
    session.flush()

    token = secrets.token_urlsafe(48)
    row = AuthSession(
        user_id=int(user.id),
        token_hash=hash_token(token),
        expires_at=now + timedelta(days=config.AUTH_SESSION_DAYS),
        last_seen_at=now,
        user_agent=(user_agent or "")[:512] or None,
        ip=(ip or "")[:128] or None,
    )
    session.add(row)
    session.flush()
    return token, row


def get_user_for_token(session: Session, token: str | None) -> User | None:
    if not token:
        return None
    row = session.exec(
        select(AuthSession).where(AuthSession.token_hash == hash_token(token))
    ).first()
    if row is None:
        return None
    now = utcnow()
    if row.expires_at <= now:
        session.delete(row)
        session.commit()
        return None
    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    # Avoid writing on every request while still keeping active sessions alive
    # in the database for audit and expiry purposes.
    if row.last_seen_at <= now - timedelta(minutes=5):
        row.last_seen_at = now
        session.add(row)
        session.commit()
    return user


def delete_session(session: Session, token: str | None) -> None:
    if not token:
        return
    row = session.exec(
        select(AuthSession).where(AuthSession.token_hash == hash_token(token))
    ).first()
    if row is not None:
        session.delete(row)
        session.commit()
