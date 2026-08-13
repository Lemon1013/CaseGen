import json

from fastapi.testclient import TestClient
import pytest
from sqlmodel import Session

import app.api.tasks as tasks_api
from app import config
from app.db import get_engine
from app.main import create_app
from app.models.entities import GenerationTask
from app.services.task_stream import TaskStreamBroker, TaskStreamEvent, TaskStreamPoll


def _create_task(client: TestClient) -> int:
    response = client.post(
        "/api/tasks",
        json={"title": "实时生成", "description": "验证 SSE 输出"},
    )
    assert response.status_code == 200
    return int(response.json()["id"])


def _sse_events(body: str) -> list[dict]:
    events: list[dict] = []
    for block in body.strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                fields[key] = value
        if "event" in fields:
            events.append(
                {
                    "id": int(fields["id"]),
                    "event": fields["event"],
                    "data": json.loads(fields["data"]),
                }
            )
    return events


def test_task_stream_returns_not_found_for_missing_task(tmp_app_data):
    client = TestClient(create_app())

    response = client.get("/api/tasks/99999/stream")

    assert response.status_code == 404
    assert response.json()["detail"] == "Task not found"


def test_task_stream_rejects_status_without_generation_producer(tmp_app_data):
    client = TestClient(create_app())
    created = client.post(
        "/api/tasks",
        headers={"Origin": "http://testserver"},
        json={"title": "实时生成", "description": "验证 SSE 输出"},
    )
    assert created.status_code == 200
    task_id = int(created.json()["id"])

    response = client.get(f"/api/tasks/{task_id}/stream")

    assert response.status_code == 409
    assert "no active generation stream" in response.json()["detail"]


def test_task_stream_rejects_active_status_when_local_broker_state_is_missing(
    tmp_app_data, monkeypatch
):
    broker = TaskStreamBroker()
    monkeypatch.setattr(tasks_api, "task_stream", broker)
    client = TestClient(create_app())
    task_id = _create_task(client)
    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        task.status = "generating"
        session.add(task)
        session.commit()

    response = client.get(f"/api/tasks/{task_id}/stream")

    assert response.status_code == 409
    assert "stream unavailable" in response.json()["detail"]
    assert broker.snapshot(task_id) is None


def test_task_stream_returns_durable_terminal_snapshot_and_headers(tmp_app_data, monkeypatch):
    broker = TaskStreamBroker()
    monkeypatch.setattr(tasks_api, "task_stream", broker)
    client = TestClient(create_app())
    task_id = _create_task(client)
    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        task.status = "generated"
        session.add(task)
        session.commit()

    response = client.get(
        f"/api/tasks/{task_id}/stream",
        headers={"Last-Event-ID": "23"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-accel-buffering"] == "no"
    assert _sse_events(response.text) == [
        {
            "id": 0,
            "event": "snapshot",
            "data": {
                "stream_id": 1,
                "sequence": 0,
                "status": "generated",
                "text": "",
                "terminal": "completed",
                "truncated": False,
            },
        }
    ]


def test_task_stream_sends_delta_and_closes_after_terminal_event(
    tmp_app_data, monkeypatch
):
    broker = TaskStreamBroker()
    monkeypatch.setattr(tasks_api, "task_stream", broker)
    client = TestClient(create_app())
    task_id = _create_task(client)
    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        task.status = "generating"
        session.add(task)
        session.commit()
    broker.start(task_id, status="generating", message="开始调用模型")
    snapshot = broker.snapshot(task_id)
    assert snapshot is not None

    def poll_after(
        task_id_arg: int,
        sequence: int,
        *,
        stream_id: int | None = None,
    ):
        assert task_id_arg == task_id
        assert sequence == snapshot["sequence"]
        assert stream_id == snapshot["stream_id"]
        return TaskStreamPoll(
            "events",
            events=(
                TaskStreamEvent(
                    sequence + 1,
                    "delta",
                    {"delta": "正在生成"},
                ),
                TaskStreamEvent(
                    sequence + 2,
                    "completed",
                    {
                        "status": "generated",
                        "message": "生成完成",
                        "text": "正在生成",
                    },
                ),
            ),
        )

    monkeypatch.setattr(broker, "poll_after", poll_after)
    response = client.get(f"/api/tasks/{task_id}/stream")

    assert response.status_code == 200
    events = _sse_events(response.text)
    assert [event["event"] for event in events] == [
        "snapshot",
        "delta",
        "completed",
    ]
    assert events[0]["data"] == snapshot
    assert events[1]["data"] == {"delta": "正在生成"}
    assert events[2]["data"]["status"] == "generated"
    assert events[2]["data"]["text"] == "正在生成"


def test_task_stream_resyncs_snapshot_after_event_overflow(tmp_app_data, monkeypatch):
    broker = TaskStreamBroker(max_events_per_task=8)
    monkeypatch.setattr(tasks_api, "task_stream", broker)
    client = TestClient(create_app())
    created = client.post(
        "/api/tasks",
        headers={"Origin": "http://testserver"},
        json={"title": "实时生成", "description": "验证 SSE 输出"},
    )
    assert created.status_code == 200
    task_id = int(created.json()["id"])
    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        task.status = "generating"
        session.add(task)
        session.commit()
    broker.start(task_id, status="generating")
    initial = broker.snapshot(task_id)
    assert initial is not None
    calls = {"n": 0}
    original_poll_after = broker.poll_after

    def overflow_then_complete(
        task_id_arg: int,
        sequence: int,
        *,
        stream_id: int | None = None,
    ) -> TaskStreamPoll:
        calls["n"] += 1
        if calls["n"] == 1:
            for _ in range(20):
                broker.delta(task_id_arg, "x")
            return original_poll_after(task_id_arg, sequence, stream_id=stream_id)
        current = broker.snapshot(task_id_arg)
        assert current is not None
        return TaskStreamPoll(
            "events",
            events=(
                TaskStreamEvent(
                    int(current["sequence"]) + 1,
                    "completed",
                    {"status": "generated", "message": "done", "text": "x" * 20},
                ),
            ),
        )

    monkeypatch.setattr(broker, "poll_after", overflow_then_complete)
    response = client.get(f"/api/tasks/{task_id}/stream")
    assert response.status_code == 200
    events = _sse_events(response.text)
    assert [event["event"] for event in events] == [
        "snapshot",
        "snapshot",
        "completed",
    ]
    assert events[1]["data"]["text"] == "x" * 20


def test_task_stream_requires_auth_and_allows_authenticated_cookie(
    tmp_app_data, monkeypatch
):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    client = TestClient(create_app())
    assert client.get("/api/tasks/1/stream").status_code == 401

    setup = client.post(
        "/api/auth/setup",
        headers={"Origin": "http://testserver"},
        json={
            "username": "admin",
            "display_name": "Admin",
            "password": "password1234",
        },
    )
    assert setup.status_code == 200
    created = client.post(
        "/api/tasks",
        headers={"Origin": "http://testserver"},
        json={"title": "实时生成", "description": "验证 SSE 输出"},
    )
    assert created.status_code == 200
    task_id = int(created.json()["id"])
    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        task.status = "generated"
        session.add(task)
        session.commit()

    response = client.get(f"/api/tasks/{task_id}/stream")
    assert response.status_code == 200
    assert _sse_events(response.text)[0]["event"] == "snapshot"


def test_delete_terminates_existing_stream_and_new_task_cannot_see_old_preview(
    tmp_app_data, monkeypatch
):
    broker = TaskStreamBroker()
    monkeypatch.setattr(tasks_api, "task_stream", broker)
    client = TestClient(create_app())
    task_id = _create_task(client)
    broker.start(task_id, status="generating")
    broker.delta(task_id, "sensitive old preview")
    old = broker.snapshot(task_id)
    assert old is not None

    deleted = client.delete(f"/api/tasks/{task_id}")
    assert deleted.status_code == 200
    poll = broker.poll_after(
        task_id,
        int(old["sequence"]),
        stream_id=int(old["stream_id"]),
    )
    assert poll.kind == "events"
    assert poll.events[-1].event == "failed"
    assert poll.events[-1].payload["message"] == "任务已删除，实时预览已关闭"
    assert client.get(f"/api/tasks/{task_id}/stream").status_code == 404

    new_task_id = _create_task(client)
    fresh = broker.snapshot(new_task_id)
    assert fresh is None
    assert client.get(f"/api/tasks/{new_task_id}/stream").status_code == 409


def test_delete_rejects_busy_task_to_prevent_queued_job_id_reuse(
    tmp_app_data, monkeypatch
):
    broker = TaskStreamBroker()
    monkeypatch.setattr(tasks_api, "task_stream", broker)
    client = TestClient(create_app())
    task_id = _create_task(client)
    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        task.status = "generating"
        session.add(task)
        session.commit()
    broker.start(task_id, status="generating")

    response = client.delete(f"/api/tasks/{task_id}")

    assert response.status_code == 409
    assert client.get(f"/api/tasks/{task_id}").status_code == 200
    snapshot = broker.snapshot(task_id)
    assert snapshot is not None
    assert snapshot["terminal"] is None


@pytest.mark.asyncio
async def test_stream_closes_database_context_before_body_iteration(monkeypatch):
    broker = TaskStreamBroker()
    broker.start(901, status="generating")
    monkeypatch.setattr(tasks_api, "task_stream", broker)
    lifecycle: list[str] = []

    class FakeSession:
        def __init__(self, engine):
            lifecycle.append("constructed")

        def __enter__(self):
            lifecycle.append("entered")
            return self

        def __exit__(self, exc_type, exc, traceback):
            lifecycle.append("closed")

        def get(self, model, task_id):
            assert model is GenerationTask
            assert task_id == 901
            return GenerationTask(id=901, requirement_id=1, status="generating")

    monkeypatch.setattr(tasks_api, "Session", FakeSession)
    monkeypatch.setattr(tasks_api, "get_engine", lambda: object())

    response = tasks_api.stream_task(901)
    assert lifecycle == ["constructed", "entered", "closed"]

    first_chunk = await anext(response.body_iterator)
    assert "event: snapshot" in first_chunk
    assert lifecycle == ["constructed", "entered", "closed"]
    await response.body_iterator.aclose()
