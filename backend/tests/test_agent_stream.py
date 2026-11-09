"""Tests for AgentStreamBroker: the deprecated-category side table and SSE shape."""

from app.services.agent_stream import AgentStreamBroker, encode_sse


def test_start_snapshot_then_complete():
    broker = AgentStreamBroker()
    broker.start("c1", status="busy", message="正在连接")
    snap = broker.snapshot("c1")
    assert snap is not None
    assert snap["status"] == "busy"
    assert snap["terminal"] is None
    assert snap["text"] == ""

    broker.complete("c1", text="完成啦", status="active")
    done = broker.snapshot("c1")
    # Agent conversations stay 'active' at terminal; only payload.terminal flips.
    assert done["status"] == "active"
    assert done["terminal"] == "completed"
    assert done["text"] == "完成啦"


def test_failed_event_and_snapshot_carry_category():
    broker = AgentStreamBroker()
    broker.start("c1", status="busy")
    broker.fail("c1", message="超时了", status="active", category="timeout")

    snap = broker.snapshot("c1")
    assert snap["terminal"] == "failed"
    assert snap["status"] == "active"
    assert snap["category"] == "timeout"

    poll = broker.poll_after("c1", 0)
    failed_frames = [e for e in poll.events if e.event == "failed"]
    assert len(failed_frames) == 1
    assert failed_frames[0].payload["category"] == "timeout"


def test_start_clears_previous_category():
    broker = AgentStreamBroker()
    broker.fail("c1", message="m", status="active", category="timeout")
    assert broker._category_locked("c1") == "timeout"

    broker.start("c1", status="busy")
    assert broker._category_locked("c1") == ""


def test_complete_clears_previous_category():
    broker = AgentStreamBroker()
    broker.fail("c1", message="m", status="active", category="timeout")
    broker.complete("c1", text="ok", status="active")
    assert broker._category_locked("c1") == ""
    snap = broker.snapshot("c1")
    assert snap["terminal"] == "completed"
    assert "category" not in snap


def test_fail_if_active_clears_category_when_not_published():
    broker = AgentStreamBroker()
    # No retained stream -> nothing is published, so the tag must not linger.
    assert broker.fail_if_active("missing", message="m", category="timeout") is False
    assert broker._category_locked("missing") == ""


def test_fail_if_active_publishes_with_category():
    broker = AgentStreamBroker()
    broker.start("c1", status="busy")
    assert broker.fail_if_active("c1", message="已取消", category="cancelled") is True
    snap = broker.snapshot("c1")
    assert snap["terminal"] == "failed"
    assert snap["category"] == "cancelled"


def test_category_side_table_is_bounded_lru():
    broker = AgentStreamBroker()
    for index in range(300):
        broker.fail(f"c{index}", message="m", status="active", category="timeout")
    assert len(broker._categories) == 256
    # Oldest keys are evicted, newest retained.
    assert "c0" not in broker._categories
    assert "c299" in broker._categories


def test_discard_removes_category():
    broker = AgentStreamBroker()
    broker.fail("c1", message="m", status="active", category="timeout")
    broker.discard("c1")
    assert broker._category_locked("c1") == ""
    assert broker.snapshot("c1") is None


def test_encode_sse_with_sequence():
    out = encode_sse("delta", {"delta": "hi"}, sequence=7)
    assert out == 'id: 7\nevent: delta\ndata: {"delta":"hi"}\n\n'


def test_encode_sse_without_sequence_and_unicode():
    out = encode_sse("notice", {"message": "你好"}, sequence=None)
    assert not out.startswith("id:")
    assert out.startswith("event: notice\n")
    assert out.endswith("\n\n")
    assert 'data: {"message":"你好"}' in out


def test_reasoning_events_and_snapshot():
    broker = AgentStreamBroker()
    broker.start("c1", status="busy")
    broker.reasoning("c1", text="先想")
    broker.reasoning("c1", text="再想", reset_answer=True)

    snap = broker.snapshot("c1")
    assert snap["reasoning"] == "先想再想"

    poll = broker.poll_after("c1", 0)
    reasoning_frames = [e for e in poll.events if e.event == "reasoning"]
    assert [e.payload["delta"] for e in reasoning_frames] == ["先想", "再想"]
    assert reasoning_frames[1].payload.get("reset_answer") is True
    assert "reset_answer" not in reasoning_frames[0].payload

    broker.complete("c1", text="答案", status="active")
    done = broker.snapshot("c1")
    assert done["terminal"] == "completed"
    assert done["reasoning"] == "先想再想"


def test_start_clears_previous_reasoning():
    broker = AgentStreamBroker()
    broker.start("c1", status="busy")
    broker.reasoning("c1", text="旧想法")
    assert broker.snapshot("c1")["reasoning"] == "旧想法"

    broker.start("c1", status="busy")
    assert "reasoning" not in broker.snapshot("c1")


def test_discard_removes_reasoning():
    broker = AgentStreamBroker()
    broker.start("c1", status="busy")
    broker.reasoning("c1", text="想法")
    broker.discard("c1")
    assert broker.snapshot("c1") is None
    assert "c1" not in broker._reasoning


def test_reasoning_ignored_after_terminal():
    broker = AgentStreamBroker()
    broker.start("c1", status="busy")
    broker.complete("c1", text="答案", status="active")
    broker.reasoning("c1", text="迟到")
    assert "reasoning" not in broker.snapshot("c1")
