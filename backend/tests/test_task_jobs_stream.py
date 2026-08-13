from __future__ import annotations

from datetime import datetime

import app.services.task_jobs as task_jobs
from app.services.task_stream import TaskStreamBroker


class _FakeTask:
    def __init__(self, status: str):
        self.status = status
        self.created_at = datetime(2026, 1, 1, 12, 0, 0)
        self.error_message: str | None = None
        self.updated_at: datetime | None = None


class _FakeSession:
    def __init__(self, status: str, task: _FakeTask | None = None):
        self._task = task if task is not None else _FakeTask(status)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def get(self, model, task_id):
        return self._task

    def add(self, task):
        pass

    def commit(self):
        pass

    def refresh(self, task):
        pass


def _raise_job_error(*args, **kwargs):
    raise RuntimeError("internal path and credentials must not reach SSE")


def test_generate_job_escape_terminates_active_stream_with_generic_message(monkeypatch):
    broker = TaskStreamBroker()
    broker.start(1, status="generating")
    broker.delta(1, "partial")
    durable_failures: list[tuple[int, str]] = []

    def record_fail(session, task_id, message):
        durable_failures.append((task_id, message))
        return True

    monkeypatch.setattr(task_jobs, "task_stream", broker)
    monkeypatch.setattr(
        task_jobs, "Session", lambda engine: _FakeSession("generating")
    )
    monkeypatch.setattr(task_jobs, "get_engine", lambda: object())
    monkeypatch.setattr(task_jobs, "run_generate", _raise_job_error)
    monkeypatch.setattr(task_jobs, "_fail_if_stuck", record_fail)
    monkeypatch.setattr(task_jobs.logger, "exception", lambda *args, **kwargs: None)

    task_jobs.job_generate(1)

    assert durable_failures and durable_failures[0][0] == 1
    snapshot = broker.snapshot(1)
    assert snapshot is not None
    assert snapshot["terminal"] == "failed"
    assert snapshot["text"] == ""
    failed_event = broker.events_after(1, 0)[-1]
    assert failed_event.event == "failed"
    assert failed_event.payload["message"] == "后台生成失败，请稍后重试"
    assert "credentials" not in failed_event.payload["message"]


def test_generate_job_escape_does_not_overwrite_completed_stream(monkeypatch):
    broker = TaskStreamBroker()
    broker.start(2, status="generating")
    broker.complete(2, text="complete")
    completed = broker.snapshot(2)
    monkeypatch.setattr(task_jobs, "task_stream", broker)
    monkeypatch.setattr(
        task_jobs, "Session", lambda engine: _FakeSession("generating")
    )
    monkeypatch.setattr(task_jobs, "get_engine", lambda: object())
    monkeypatch.setattr(task_jobs, "run_generate", _raise_job_error)
    monkeypatch.setattr(task_jobs, "_fail_if_stuck", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_jobs.logger, "exception", lambda *args, **kwargs: None)

    task_jobs.job_generate(2)

    assert broker.snapshot(2) == completed


def test_regenerate_job_escape_terminates_active_stream(monkeypatch):
    broker = TaskStreamBroker()
    broker.start(3, status="regenerating")
    monkeypatch.setattr(task_jobs, "task_stream", broker)
    monkeypatch.setattr(
        task_jobs, "Session", lambda engine: _FakeSession("regenerating")
    )
    monkeypatch.setattr(task_jobs, "get_engine", lambda: object())
    monkeypatch.setattr(task_jobs, "run_regenerate", _raise_job_error)
    monkeypatch.setattr(task_jobs, "_fail_if_stuck", lambda *args, **kwargs: True)
    monkeypatch.setattr(task_jobs.logger, "exception", lambda *args, **kwargs: None)

    task_jobs.job_regenerate(3)

    snapshot = broker.snapshot(3)
    assert snapshot is not None
    assert snapshot["terminal"] == "failed"


def test_generate_job_claim_skips_without_active_stream(monkeypatch):
    broker = TaskStreamBroker()
    calls: list[int] = []
    monkeypatch.setattr(task_jobs, "task_stream", broker)
    monkeypatch.setattr(
        task_jobs, "Session", lambda engine: _FakeSession("generating")
    )
    monkeypatch.setattr(task_jobs, "get_engine", lambda: object())
    monkeypatch.setattr(
        task_jobs,
        "run_generate",
        lambda session, task_id, chat_fn=None: calls.append(task_id),
    )

    task_jobs.job_generate(4)

    assert calls == []
    assert broker.snapshot(4) is None


def test_generate_job_claim_skips_when_status_moved_on(monkeypatch):
    broker = TaskStreamBroker()
    broker.start(5, status="generating")
    calls: list[int] = []
    monkeypatch.setattr(task_jobs, "task_stream", broker)
    monkeypatch.setattr(task_jobs, "Session", lambda engine: _FakeSession("failed"))
    monkeypatch.setattr(task_jobs, "get_engine", lambda: object())
    monkeypatch.setattr(
        task_jobs,
        "run_generate",
        lambda session, task_id, chat_fn=None: calls.append(task_id),
    )

    task_jobs.job_generate(5)

    assert calls == []
    assert broker.snapshot(5)["terminal"] is None


def test_mark_job_failed_ignores_replacement_task(monkeypatch):
    broker = TaskStreamBroker()
    broker.start(6, status="generating")
    replacement = _FakeTask("generating")
    replacement.created_at = datetime(2030, 1, 1)
    durable_failures: list[tuple[int, str]] = []
    monkeypatch.setattr(task_jobs, "task_stream", broker)
    monkeypatch.setattr(
        task_jobs,
        "Session",
        lambda engine: _FakeSession("generating", task=replacement),
    )
    monkeypatch.setattr(task_jobs, "get_engine", lambda: object())
    monkeypatch.setattr(
        task_jobs,
        "_fail_if_stuck",
        lambda session, task_id, message: durable_failures.append((task_id, message)),
    )

    task_jobs._mark_job_failed(
        6,
        durable_message="late failure",
        stream_message="late failure",
        expected_stream_id=broker.snapshot(6)["stream_id"],
        launch_created_at=datetime(2020, 1, 1),
    )

    assert durable_failures == []
    assert broker.snapshot(6)["terminal"] is None


def test_claim_auto_review_skips_replacement_task(monkeypatch):
    replacement = _FakeTask("generated")
    replacement.created_at = datetime(2030, 1, 1)
    monkeypatch.setattr(
        task_jobs,
        "Session",
        lambda engine: _FakeSession("generated", task=replacement),
    )
    monkeypatch.setattr(task_jobs, "get_engine", lambda: object())

    assert task_jobs._claim_auto_review(7, datetime(2020, 1, 1)) is False


def test_claim_auto_review_accepts_matching_task(monkeypatch):
    monkeypatch.setattr(
        task_jobs,
        "Session",
        lambda engine: _FakeSession("generated"),
    )
    monkeypatch.setattr(task_jobs, "get_engine", lambda: object())
    monkeypatch.setattr(task_jobs, "append_event", lambda *args, **kwargs: None)

    assert task_jobs._claim_auto_review(8, datetime(2026, 1, 1, 12, 0, 0)) is True
