"""Shared role and permission helpers for the trusted CaseGen API.

All role checks funnel through :func:`require_admin` so that the API modules
which previously carried their own `_require_admin` copy stay in sync.  The
helpers deliberately tolerate `request.state.user is None`:
`tests/conftest.py` disables authentication for the legacy pipeline suite,
and the global middleware only populates `request.state.user` when
`config.AUTH_ENABLED` is true.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from fastapi import HTTPException, Request

from app import config
from app.models.entities import User

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = frozenset({ROLE_ADMIN, ROLE_USER})

_ADMIN_REQUIRED_DETAIL = "Administrator access required"


@dataclass(frozen=True, slots=True)
class AuthIdentity:
    """Session-independent snapshot of the authenticated user.

    The authentication middleware loads a SQLModel ``User`` inside a short-lived
    ``Session`` and closes that session before the request reaches any route.
    Keeping the ORM instance on ``request.state.user`` leaves it detached with
    expired attributes: a later ``getattr(user, "is_active")`` tries to lazily
    refresh from the closed session and raises ``DetachedInstanceError`` (a
    real 500 under uvicorn, invisible to TestClient's object lifecycle).

    This immutable, slotted record holds plain Python values only, so no
    attribute access can ever trigger a database load.  It exposes the same
    attribute names every caller already reads via ``getattr``.
    """

    id: int
    username: str
    display_name: str
    role: str
    is_active: bool


def snapshot_user(user: User) -> AuthIdentity:
    """Freeze an ORM User into a detached-safe identity.

    MUST be called while the owning ``Session`` is still open: every attribute
    is read eagerly here, inside the session, so nothing is left to load later.
    """

    return AuthIdentity(
        id=int(user.id or 0),
        username=str(user.username or ""),
        display_name=str(user.display_name or ""),
        role=str(user.role or ""),
        is_active=bool(user.is_active),
    )


# Callers may still see a live ORM User (e.g. /api/auth/session and /me build
# their own from the request Session), so both shapes are accepted.
Identity = Union[AuthIdentity, User]


def current_user(request: Request) -> Optional[Identity]:
    """Return the authenticated identity, or `None` when auth is off/anonymous.

    Never raises: callers that need a hard boundary use :func:`require_admin`.
    """

    return getattr(request.state, "user", None)


def is_admin(request: Request) -> bool:
    """Whether the current request carries an active administrator identity."""

    user = current_user(request)
    if user is None:
        return False
    return bool(getattr(user, "is_active", True)) and getattr(user, "role", "") == ROLE_ADMIN


def require_admin(request: Request, *, detail: str = _ADMIN_REQUIRED_DETAIL) -> None:
    """Reject non-administrators with 403.

    When authentication is disabled (legacy test compatibility mode) the check
    is a no-op, preserving the behaviour of the former per-module helpers.
    An anonymous request in an auth-enabled deployment is rejected rather than
    treated as an implicit administrator.
    """

    if not config.AUTH_ENABLED:
        return
    if is_admin(request):
        return
    raise HTTPException(status_code=403, detail=detail)


def require_admin_purge(request: Request) -> None:
    """Purge-specific boundary: unavailable (503) while auth is disabled.

    `/api/admin/wiki/purge` is a destructive, irreversible operation and
    historically refused to run at all when the compatibility switch is on;
    keep that stronger guard instead of silently allowing it.
    """

    if not config.AUTH_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Administrator purge is unavailable while authentication is disabled",
        )
    require_admin(request, detail="Administrator access required")
