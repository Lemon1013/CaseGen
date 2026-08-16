import pytest

from app.services.task_state import InvalidTransition, can_transition, transition


def test_allowed_happy_path():
    """The complete-case stage is reachable only after test-point confirmation."""
    path = [
        "draft",
        "retrieving",
        "awaiting_confirmation",
        "generating_test_points",
        "awaiting_test_point_confirmation",
        "generating",
        "generated",
        "reviewing",
        "reviewed",
        "finalized",
    ]
    for current, new in zip(path, path[1:]):
        assert can_transition(current, new) is True
        assert transition(current, new) == new


def test_disallow_skip_draft_to_finalized():
    assert can_transition("draft", "finalized") is False
    with pytest.raises(InvalidTransition):
        transition("draft", "finalized")


def test_disallow_complete_generation_bypass_from_retrieval_gate():
    assert can_transition("retrieving", "generating") is False
    assert can_transition("awaiting_confirmation", "generating") is False
