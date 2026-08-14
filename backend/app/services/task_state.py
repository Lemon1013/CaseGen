from __future__ import annotations

ALLOWED = {
    "draft": {"retrieving", "failed"},
    "retrieving": {"awaiting_confirmation", "generating", "failed"},
    "awaiting_confirmation": {"generating", "retrieving", "failed"},
    "generating": {"generated", "failed"},
    "generated": {"reviewing", "regenerating", "finalized", "failed"},
    "reviewing": {"reviewed", "failed"},
    "reviewed": {"optimizing", "regenerating", "finalized", "failed"},
    "optimizing": {"reviewed", "failed"},
    "regenerating": {"retrieving", "generating", "failed"},
    "finalized": set(),
    "failed": {"retrieving", "generating", "reviewing", "optimizing", "regenerating"},
}


class InvalidTransition(Exception):
    """Raised when a task status transition is not allowed."""


def can_transition(current: str, new: str) -> bool:
    return new in ALLOWED.get(current, set())


def transition(current: str, new: str) -> str:
    if not can_transition(current, new):
        raise InvalidTransition(f"Cannot transition from {current!r} to {new!r}")
    return new
