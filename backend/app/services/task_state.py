from __future__ import annotations

ALLOWED = {
    "draft": {"retrieving", "failed"},
    "retrieving": {"awaiting_confirmation", "failed"},
    # This status remains reserved for the retrieval checkpoint.  The next
    # durable stage is intentionally distinct so an old recovery worker cannot
    # mistake a test-point decision for evidence confirmation.
    "awaiting_confirmation": {"generating_test_points", "retrieving", "failed"},
    "generating_test_points": {"awaiting_test_point_confirmation", "failed"},
    "awaiting_test_point_confirmation": {"generating_test_points", "generating", "failed"},
    # A pre-checkpoint worker may have persisted this legacy state before the
    # test-point gate existed.  Recovery is allowed to move it to the durable
    # test-point stage, never directly to complete-case generation.
    "generating": {"generating_test_points", "retrieving", "generated", "failed"},
    "generated": {"reviewing", "regenerating", "finalized", "failed"},
    "reviewing": {"reviewed", "failed"},
    "reviewed": {"optimizing", "regenerating", "finalized", "failed"},
    "optimizing": {"reviewed", "failed"},
    "regenerating": {"retrieving", "failed"},
    "finalized": set(),
    "failed": {
        "retrieving",
        "generating_test_points",
        "reviewing",
        "optimizing",
        "regenerating",
    },
}


class InvalidTransition(Exception):
    """Raised when a task status transition is not allowed."""


def can_transition(current: str, new: str) -> bool:
    return new in ALLOWED.get(current, set())


def transition(current: str, new: str) -> str:
    if not can_transition(current, new):
        raise InvalidTransition(f"Cannot transition from {current!r} to {new!r}")
    return new
