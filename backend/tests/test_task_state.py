import pytest

from app.services.task_state import InvalidTransition, can_transition, transition


def test_allowed_happy_path():
    """draft → retrieving → generating → generated → reviewing → reviewed → finalized"""
    path = [
        "draft",
        "retrieving",
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
