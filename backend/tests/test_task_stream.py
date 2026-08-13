from __future__ import annotations

import threading

import app.services.task_stream as task_stream_module
from app.services.task_stream import TaskStreamBroker


def test_broker_accumulates_snapshot_and_resets_retried_attempt():
    broker = TaskStreamBroker()
    broker.start(7, status="generating", message="start")
    broker.delta(7, "old partial")

    before = broker.snapshot(7)
    assert before is not None
    assert before["text"] == "old partial"

    broker.retry(7, attempt=2, message="retrying")
    broker.reset(7, status="generating", message="replace attempt")
    broker.delta(7, "new ")
    broker.delta(7, "complete")
    broker.complete(7, text="new complete")

    snapshot = broker.snapshot(7)
    assert snapshot is not None
    assert snapshot["text"] == "new complete"
    assert snapshot["terminal"] == "completed"
    events = broker.events_after(7, 0)
    assert [event.event for event in events] == [
        "reset",
        "status",
        "delta",
        "retry",
        "reset",
        "delta",
        "delta",
        "completed",
    ]


def test_broker_bounds_tasks_and_per_task_event_history():
    broker = TaskStreamBroker(max_tasks=2, max_events_per_task=8)
    broker.start(1, status="generating")
    broker.start(2, status="generating")
    # Reading task 1 makes it the most recently used state; adding task 3
    # should evict task 2 while preserving the bounded accumulated snapshot.
    assert broker.snapshot(1) is not None
    broker.start(3, status="generating")
    assert tuple(broker.task_ids()) == (1, 3)
    assert broker.snapshot(2) is None

    for _ in range(20):
        broker.delta(3, "x")
    assert len(broker.events_after(3, 0)) == 8
    assert broker.snapshot(3)["text"] == "x" * 20  # type: ignore[index]


def test_broker_bounds_total_event_payload_history_chars():
    broker = TaskStreamBroker(
        max_events_per_task=512,
        max_event_text_chars=100,
        max_event_history_chars=160,
        max_preview_chars=10_000,
    )
    broker.start(4, status="generating")
    start_sequence = int(broker.snapshot(4)["sequence"])  # type: ignore[index]
    for _ in range(20):
        broker.delta(4, "x" * 100)

    retained = broker.events_after(4, 0)
    retained_chars = sum(
        broker._event_payload_chars(event.payload)  # noqa: SLF001 - budget assertion
        for event in retained
    )
    assert retained_chars <= broker.max_event_history_chars
    assert len(retained) < 20
    assert broker.poll_after(4, start_sequence).kind == "snapshot"


def test_terminal_snapshot_expires_after_ttl(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(task_stream_module.time, "monotonic", lambda: clock[0])
    broker = TaskStreamBroker(terminal_ttl_sec=1.0)
    broker.start(9, status="generating")
    broker.delta(9, "done")
    broker.complete(9, text="done")

    clock[0] = 100.9
    assert broker.snapshot(9) is not None
    clock[0] = 101.1
    assert broker.snapshot(9) is None
    assert tuple(broker.task_ids()) == ()


def test_broker_wakes_waiter_and_serializes_concurrent_deltas():
    broker = TaskStreamBroker(max_events_per_task=512)
    broker.start(11, status="generating")
    sequence = int(broker.snapshot(11)["sequence"])  # type: ignore[index]
    ready = threading.Event()
    received: list[str] = []

    def wait_for_delta() -> None:
        ready.set()
        result = broker.wait_for_events(11, sequence, timeout_sec=1.0)
        assert result.kind == "events"
        received.extend(event.event for event in result.events)

    waiter = threading.Thread(target=wait_for_delta)
    waiter.start()
    assert ready.wait(timeout=1.0)
    broker.delta(11, "w")
    waiter.join(timeout=1.0)
    assert not waiter.is_alive()
    assert received == ["delta"]

    writers = [
        threading.Thread(
            target=lambda value=value: [broker.delta(11, value) for _ in range(50)]
        )
        for value in ("a", "b", "c", "d")
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(timeout=2.0)
        assert not writer.is_alive()

    text = str(broker.snapshot(11)["text"])  # type: ignore[index]
    assert text.count("w") == 1
    for value in ("a", "b", "c", "d"):
        assert text.count(value) == 50


def test_preview_and_event_payloads_are_bounded_with_clear_truncation_notice():
    broker = TaskStreamBroker(
        max_preview_chars=10,
        max_event_text_chars=4,
        max_message_chars=20,
    )
    broker.start(20, status="generating")
    broker.delta(20, "abcdefghijklm")
    # Further upstream output is ignored once the live preview reaches its
    # cap, keeping both accumulated state and event history bounded.
    broker.delta(20, "ignored")
    broker.complete(20, text="0123456789" + "z" * 100, message="done")

    snapshot = broker.snapshot(20)
    assert snapshot is not None
    assert snapshot["text"] == "0123456789"
    assert snapshot["truncated"] is True
    assert "完整草稿" in snapshot["message"]

    events = broker.events_after(20, 0)
    delta_payloads = [event.payload["delta"] for event in events if event.event == "delta"]
    assert delta_payloads == ["abcd", "efgh", "ij"]
    assert all(len(delta) <= 4 for delta in delta_payloads)
    notices = [event for event in events if event.event == "notice"]
    assert len(notices) == 1
    completed = [event for event in events if event.event == "completed"][-1]
    assert "text" not in completed.payload
    assert completed.payload["truncated"] is True
    assert len(completed.payload["message"]) <= 20


def test_poll_returns_snapshot_when_event_history_has_overflowed():
    broker = TaskStreamBroker(max_events_per_task=8)
    broker.start(30, status="generating")
    initial = broker.snapshot(30)
    assert initial is not None
    for _ in range(20):
        broker.delta(30, "x")

    result = broker.poll_after(
        30,
        int(initial["sequence"]),
        stream_id=int(initial["stream_id"]),
    )
    assert result.kind == "snapshot"
    assert result.snapshot is not None
    assert result.snapshot["text"] == "x" * 20
    assert result.snapshot["sequence"] > initial["sequence"]


def test_poll_signals_eviction_and_replacement_instead_of_waiting_forever():
    broker = TaskStreamBroker(max_tasks=1)
    broker.start(40, status="generating")
    old = broker.snapshot(40)
    assert old is not None

    broker.start(41, status="generating")
    evicted = broker.wait_for_events(
        40,
        int(old["sequence"]),
        stream_id=int(old["stream_id"]),
        timeout_sec=0,
    )
    assert evicted.kind == "missing"

    broker.discard(41)
    replaced = broker.poll_after(
        41,
        0,
        stream_id=1,
    )
    assert replaced.kind == "missing"
    assert broker.snapshot(41) is None


def test_fail_if_active_terminates_only_unfinished_stream():
    broker = TaskStreamBroker()
    broker.start(50, status="generating")
    broker.delta(50, "partial")
    assert broker.fail_if_active(50, message="job failed") is True
    failed = broker.snapshot(50)
    assert failed is not None
    assert failed["terminal"] == "failed"
    assert failed["text"] == ""

    broker.start(51, status="generating")
    broker.complete(51, text="complete")
    completed = broker.snapshot(51)
    assert broker.fail_if_active(51, message="late review failed") is False
    assert broker.snapshot(51) == completed

    broker.start(52, status="generating")
    old = broker.snapshot(52)
    assert old is not None
    broker.discard(52)
    broker.start(52, status="generating")
    assert broker.fail_if_active(
        52,
        message="stale delete",
        expected_stream_id=int(old["stream_id"]),
    ) is False
    fresh = broker.snapshot(52)
    assert fresh is not None
    assert fresh["status"] == "generating"
    assert fresh["terminal"] is None


def test_discarded_drafts_do_not_consume_broker_capacity():
    broker = TaskStreamBroker(max_tasks=2)
    broker.start(60, status="generating")
    active = broker.snapshot(60)
    assert active is not None

    for task_id in range(100, 500):
        broker.discard(task_id)

    assert tuple(broker.task_ids()) == (60,)
    assert broker.snapshot(60) == active


def test_missing_old_state_never_authorizes_failing_reused_task_id():
    broker = TaskStreamBroker()
    assert broker.snapshot(70) is None
    expected_stream_id = None

    # Simulate a new same-id task starting between the old delete commit and
    # its broker cleanup. The delete route skips fail_if_active when it had no
    # old identity to match.
    broker.start(70, status="generating")
    if expected_stream_id is not None:
        broker.fail_if_active(
            70,
            message="stale delete",
            expected_stream_id=expected_stream_id,
        )

    snapshot = broker.snapshot(70)
    assert snapshot is not None
    assert snapshot["terminal"] is None
