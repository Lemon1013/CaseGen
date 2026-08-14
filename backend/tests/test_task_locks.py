from __future__ import annotations

import threading
import time

from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from sqlmodel import Session

import app.api.tasks as tasks_api
from app.db import get_engine
from app.main import create_app
from app.services.task_locks import TaskLockRegistry


def test_task_lock_serializes_same_task_and_cleans_registry():
    registry = TaskLockRegistry()
    entered: list[str] = []
    first_holds = threading.Event()
    release_first = threading.Event()

    def first() -> None:
        with registry.hold(1):
            entered.append("first")
            first_holds.set()
            assert release_first.wait(timeout=1.0)

    def second() -> None:
        assert first_holds.wait(timeout=1.0)
        with registry.hold(1):
            entered.append("second")

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert first_holds.wait(timeout=1.0)
    time.sleep(0.02)
    assert entered == ["first"]
    release_first.set()
    first_thread.join(timeout=1.0)
    second_thread.join(timeout=1.0)

    assert entered == ["first", "second"]
    assert registry.size() == 0


def test_concurrent_generate_requests_enqueue_only_one_background_job(
    tmp_app_data, monkeypatch
):
    client = TestClient(create_app())
    model = client.post(
        "/api/models",
        json={
            "name": "lock-model",
            "base_url": "https://example.test/v1",
            "api_key": "sk-lock",
            "model_name": "fake",
            "is_default": True,
        },
    ).json()
    task_id = int(
        client.post(
            "/api/tasks",
            json={"title": "并发生成", "description": "只允许一个后台任务", "model_id": model["id"]},
        ).json()["id"]
    )
    monkeypatch.setattr(tasks_api, "_PIPELINE_CHAT_FN", None)
    monkeypatch.setattr(tasks_api, "_GENERATE_CHAT_FN", None)
    start = threading.Barrier(3)
    scheduled: list[int] = []
    statuses: list[str] = []
    errors: list[BaseException] = []

    def invoke() -> None:
        background = BackgroundTasks()
        try:
            with Session(get_engine()) as session:
                start.wait(timeout=2.0)
                result = tasks_api.generate_task(
                    task_id,
                    background,
                    session,
                    wait=False,
                    auto_review=False,
                )
                statuses.append(result.status)
                scheduled.append(len(background.tasks))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=invoke) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait(timeout=2.0)
    for thread in threads:
        thread.join(timeout=3.0)
        assert not thread.is_alive()

    assert errors == []
    assert sorted(scheduled) == [0, 1]
    assert statuses == ["retrieving", "retrieving"]
