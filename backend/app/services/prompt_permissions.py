"""Role and visibility permission helpers for PromptTemplate records."""

from __future__ import annotations

from app import config
from app.models.entities import PromptTemplate
from app.services.permissions import Identity, ROLE_ADMIN
from app.services.prompts_seed import USER_MANAGEABLE_PROMPT_TYPES


def resolve_prompt_actor(user: Identity | None) -> tuple[bool, int | None]:
    """Return ``(effective_admin, effective_user_id)`` for prompt governance.

    In legacy test compatibility mode (``not config.AUTH_ENABLED`` and
    ``user is None``), callers are treated as a system administrator context
    with no concrete user id.  When an authenticated identity is present,
    its active status, role and user id are authoritative.
    """
    if user is None:
        if not config.AUTH_ENABLED:
            return True, None
        return False, None
    if not bool(getattr(user, "is_active", True)):
        return False, None
    is_admin_user = getattr(user, "role", "") == ROLE_ADMIN
    raw_id = getattr(user, "id", None)
    user_id = int(raw_id) if raw_id is not None else None
    return is_admin_user, user_id


def _normalized_visibility(row: PromptTemplate) -> str:
    value = (getattr(row, "visibility", None) or "system").strip()
    return value or "system"


def can_view_prompt(row: PromptTemplate, user: Identity | None) -> bool:
    """Whether ``user`` may see or select ``row``.

    - Non-``generate`` (system engine) prompts are visible to administrators only.
    - ``generate`` prompts with ``system`` or ``shared`` visibility are visible
      to all signed-in users.
    - ``generate`` prompts with ``private`` visibility are strictly visible
      ONLY to their owner (``row.owner_user_id == user.id``).  Not even
      administrators may view another user's private prompt.
    """
    if user is not None and not bool(getattr(user, "is_active", True)):
        return False
    if config.AUTH_ENABLED and user is None:
        return False

    effective_admin, effective_user_id = resolve_prompt_actor(user)
    if row.type not in USER_MANAGEABLE_PROMPT_TYPES:
        return effective_admin

    visibility = _normalized_visibility(row)
    if visibility in {"system", "shared"}:
        return True
    if visibility == "private":
        if user is not None:
            return (
                row.owner_user_id is not None
                and effective_user_id is not None
                and int(row.owner_user_id) == effective_user_id
            )
        # Compatibility mode: AUTH_ENABLED is False and user is None.
        return row.owner_user_id is None
    return False


def can_manage_prompt(row: PromptTemplate, user: Identity | None) -> bool:
    """Whether ``user`` may edit or delete ``row``."""
    if not can_view_prompt(row, user):
        return False

    effective_admin, effective_user_id = resolve_prompt_actor(user)
    visibility = _normalized_visibility(row)

    if row.type not in USER_MANAGEABLE_PROMPT_TYPES or visibility == "system":
        return effective_admin

    if visibility == "private":
        if user is not None:
            return (
                row.owner_user_id is not None
                and effective_user_id is not None
                and int(row.owner_user_id) == effective_user_id
            )
        return row.owner_user_id is None

    if visibility == "shared":
        if effective_admin:
            return True
        return (
            row.owner_user_id is not None
            and effective_user_id is not None
            and int(row.owner_user_id) == effective_user_id
        )

    return False


def can_fork_prompt(row: PromptTemplate, user: Identity | None) -> bool:
    """Whether ``user`` may fork ``row`` into a new generate prompt."""
    return row.type in USER_MANAGEABLE_PROMPT_TYPES and can_view_prompt(row, user)
