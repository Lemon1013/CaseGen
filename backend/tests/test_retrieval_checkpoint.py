import json
import time

import threading

from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_engine
from app.main import create_app
from app.models.entities import CaseDraft, GenerationTask, TaskRetrievalCheckpoint


def _model(client: TestClient) -> int:
    response = client.post(
        "/api/models",
        json={
            "name": "checkpoint-model",
            "base_url": "https://example.test/v1",
            "api_key": "sk-test",
            "model_name": "fake",
            "is_default": True,
        },
    )
    assert response.status_code == 200
    return response.json()["id"]


def _task(client: TestClient, model_id: int | None = None) -> int:
    body = {"title": "余额规则", "description": "余额不足时拒绝下单"}
    if model_id is not None:
        body["model_id"] = model_id
    response = client.post("/api/tasks", json=body)
    assert response.status_code == 200
    return response.json()["id"]


def _retrieve_hits(*_args, **_kwargs):
    return {
        "wiki_hits": [
            {"id": 101, "title": "同名规则", "path": "pages/a.md", "content": "A 规则内容", "score": 0.9},
            {"id": 102, "title": "同名规则", "path": "pages/b.md", "content": "B 规则内容", "score": 0.8},
        ],
        "source_hits": [],
    }


def _confirm(client: TestClient, task_id: int, selected: list[int], supplemental: str = "", **extra):
    checkpoint = client.get(f"/api/tasks/{task_id}/retrieval-checkpoint").json()
    payload = {
        "selected_citation_ids": selected,
        "supplemental_text": supplemental,
        "expected_version": checkpoint["version"],
        "idempotency_key": f"checkpoint-test-{task_id}",
    }
    payload.update(extra)
    response = client.post(f"/api/tasks/{task_id}/retrieval-checkpoint/confirm", json=payload)
    if response.status_code == 200:
        for _ in range(80):
            current = client.get(f"/api/tasks/{task_id}").json()
            if current["status"] not in {"generating", "retrieving"}:
                return type("Response", (), {"status_code": 200, "json": lambda self: current})()
            time.sleep(0.05)
    return response


def test_retrieval_stops_before_chat_and_get_restores_checkpoint(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    model_id = _model(client)
    task_id = _task(client, model_id)
    retrieve_calls = {"count": 0}
    chat_calls = {"count": 0}

    def retrieve(*args, **kwargs):
        retrieve_calls["count"] += 1
        return _retrieve_hits()

    def chat(**kwargs):
        chat_calls["count"] += 1
        return "# 用例\n生成完成"

    monkeypatch.setattr("app.services.hybrid_retrieve.hybrid_retrieve", retrieve)
    monkeypatch.setattr("app.api.tasks._GENERATE_CHAT_FN", chat)
    monkeypatch.setattr("app.services.task_pipeline._GENERATE_CHAT_FN", chat)
    response = client.post(f"/api/tasks/{task_id}/generate")
    assert response.json()["status"] == "awaiting_confirmation"
    assert retrieve_calls["count"] == 1
    assert chat_calls["count"] == 0
    checkpoint = client.get(f"/api/tasks/{task_id}/retrieval-checkpoint")
    assert checkpoint.status_code == 200
    assert checkpoint.json()["status"] == "pending"
    assert len(checkpoint.json()["candidate_citations"]) == 2


def test_confirm_subset_and_supplemental_resumes_once_without_retrieve(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    model_id = _model(client)
    task_id = _task(client, model_id)
    calls = {"retrieve": 0, "chat": 0, "messages": []}

    def retrieve(*args, **kwargs):
        calls["retrieve"] += 1
        return _retrieve_hits()

    def chat(**kwargs):
        calls["chat"] += 1
        calls["messages"].append(kwargs["messages"])
        return "# 已确认用例\n生成完成"

    monkeypatch.setattr("app.services.hybrid_retrieve.hybrid_retrieve", retrieve)
    monkeypatch.setattr("app.api.tasks._GENERATE_CHAT_FN", chat)
    monkeypatch.setattr("app.services.task_pipeline._GENERATE_CHAT_FN", chat)
    assert client.post(f"/api/tasks/{task_id}/generate").json()["status"] == "awaiting_confirmation"
    checkpoint = client.get(f"/api/tasks/{task_id}/retrieval-checkpoint").json()
    second_id = checkpoint["candidate_citations"][1]["id"]
    response = _confirm(client, task_id, [second_id], "只关注 B 规则")
    assert response.status_code == 200
    assert response.json()["status"] == "generated", {
        "task": response.json(),
        "events": client.get(f"/api/tasks/{task_id}/events").json(),
    }
    assert calls["retrieve"] == 1
    assert calls["chat"] == 1
    joined = "\n".join(item["content"] for item in calls["messages"][0])
    assert "B 规则内容" in joined
    assert "A 规则内容" not in joined
    assert "只关注 B 规则" in joined


def test_duplicate_titles_use_distinct_citation_identity(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    model_id = _model(client)
    task_id = _task(client, model_id)
    messages: list[str] = []

    monkeypatch.setattr("app.services.hybrid_retrieve.hybrid_retrieve", lambda *a, **k: _retrieve_hits())
    monkeypatch.setattr(
        "app.api.tasks._GENERATE_CHAT_FN",
        lambda **kwargs: messages.append("\n".join(x["content"] for x in kwargs["messages"])) or "# 完成",
    )
    monkeypatch.setattr(
        "app.services.task_pipeline._GENERATE_CHAT_FN",
        lambda **kwargs: messages.append("\n".join(x["content"] for x in kwargs["messages"])) or "# 完成",
    )
    client.post(f"/api/tasks/{task_id}/generate")
    cp = client.get(f"/api/tasks/{task_id}/retrieval-checkpoint").json()
    _confirm(client, task_id, [cp["candidate_citations"][1]["id"]])
    assert "B 规则内容" in messages[0]
    assert "A 规则内容" not in messages[0]


def test_checkpoint_validation_idempotency_and_cross_task(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    model_id = _model(client)
    first = _task(client, model_id)
    second = _task(client, model_id)
    monkeypatch.setattr("app.services.hybrid_retrieve.hybrid_retrieve", lambda *a, **k: _retrieve_hits())
    monkeypatch.setattr("app.api.tasks._GENERATE_CHAT_FN", lambda **kwargs: "# 完成")
    client.post(f"/api/tasks/{first}/generate")
    client.post(f"/api/tasks/{second}/generate")
    first_cp = client.get(f"/api/tasks/{first}/retrieval-checkpoint").json()
    second_cp = client.get(f"/api/tasks/{second}/retrieval-checkpoint").json()
    empty = _confirm(client, first, [])
    assert empty.status_code == 422
    cross = _confirm(client, first, [second_cp["candidate_citations"][0]["id"]])
    assert cross.status_code == 422
    stale = _confirm(client, first, [first_cp["candidate_citations"][0]["id"]], expected_version=999)
    assert stale.status_code == 409
    confirmed = _confirm(client, first, [first_cp["candidate_citations"][0]["id"]])
    assert confirmed.status_code == 200
    repeated = client.post(
        f"/api/tasks/{first}/retrieval-checkpoint/confirm",
        json={
            "selected_citation_ids": [first_cp["candidate_citations"][0]["id"]],
            "supplemental_text": "",
            "expected_version": first_cp["version"],
            "idempotency_key": f"checkpoint-test-{first}",
        },
    )
    assert repeated.status_code == 200


def test_no_model_fails_before_retrieval_or_checkpoint(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    task_id = _task(client)
    called = {"retrieve": False}
    monkeypatch.setattr("app.services.hybrid_retrieve.hybrid_retrieve", lambda *a, **k: called.__setitem__("retrieve", True))
    response = client.post(f"/api/tasks/{task_id}/generate")
    assert response.json()["status"] == "failed"
    assert called["retrieve"] is False
    assert client.get(f"/api/tasks/{task_id}/retrieval-checkpoint").status_code == 404


def test_auto_review_is_persisted_on_pending_checkpoint(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    model_id = _model(client)
    task_id = _task(client, model_id)
    monkeypatch.setattr("app.services.hybrid_retrieve.hybrid_retrieve", lambda *a, **k: _retrieve_hits())
    monkeypatch.setattr("app.api.tasks._GENERATE_CHAT_FN", lambda **kwargs: "# 完成")
    response = client.post(f"/api/tasks/{task_id}/generate?auto_review=true")
    assert response.json()["status"] == "awaiting_confirmation"
    with Session(get_engine()) as session:
        checkpoint = session.get(TaskRetrievalCheckpoint, client.get(f"/api/tasks/{task_id}/retrieval-checkpoint").json()["id"])
        assert checkpoint is not None
        assert checkpoint.auto_review is True


def test_concurrent_confirm_schedules_one_job(tmp_app_data, monkeypatch):
    import app.api.tasks as tasks_api
    from app.schemas.tasks import RetrievalCheckpointConfirm

    client = TestClient(create_app())
    model_id = _model(client)
    task_id = _task(client, model_id)
    monkeypatch.setattr("app.services.hybrid_retrieve.hybrid_retrieve", lambda *a, **k: _retrieve_hits())
    monkeypatch.setattr("app.api.tasks._GENERATE_CHAT_FN", lambda **kwargs: "# 完成")
    monkeypatch.setattr("app.services.task_pipeline._GENERATE_CHAT_FN", lambda **kwargs: "# 完成")
    assert client.post(f"/api/tasks/{task_id}/generate").json()["status"] == "awaiting_confirmation"
    checkpoint = client.get(f"/api/tasks/{task_id}/retrieval-checkpoint").json()
    body = RetrievalCheckpointConfirm(
        selected_citation_ids=[checkpoint["candidate_citations"][0]["id"]],
        supplemental_text="",
        expected_version=checkpoint["version"],
        idempotency_key="concurrent-confirm",
    )
    scheduled: list[int] = []
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            background = BackgroundTasks()
            with Session(get_engine()) as session:
                tasks_api.confirm_retrieval_checkpoint(task_id, body, background, session)
            scheduled.append(len(background.tasks))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=invoke) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert errors == []
    assert sorted(scheduled) == [0, 1]


def test_startup_recovery_only_schedules_confirmed_without_draft(tmp_app_data, monkeypatch):
    import app.services.task_jobs as task_jobs

    client = TestClient(create_app())
    model_id = _model(client)
    task_id = _task(client, model_id)
    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        task.status = "generating"
        checkpoint = TaskRetrievalCheckpoint(
            task_id=task_id,
            attempt=1,
            status="confirmed",
            selected_citation_ids_json="[]",
            candidate_citation_ids_json="[]",
            retrieval_json=json.dumps({"context": {}}),
        )
        session.add(checkpoint)
        session.commit()

    started: list[tuple[int, bool]] = []

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            self.args = args

        def start(self):
            started.append((int(self.args[0]), bool(self.args[1])))

    monkeypatch.setattr(task_jobs.threading, "Thread", FakeThread)
    assert task_jobs.recover_generation_jobs() == [task_id]
    assert started == [(task_id, False)]
    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        session.add(CaseDraft(task_id=task_id, version=1, content_md="# existing"))
        session.commit()
    started.clear()
    assert task_jobs.recover_generation_jobs() == []
    assert started == []


def test_recovery_uses_latest_checkpoint_before_lease_eligibility(tmp_app_data, monkeypatch):
    import app.services.task_jobs as task_jobs
    from datetime import datetime, timedelta

    client = TestClient(create_app())
    model_id = _model(client)
    task_id = _task(client, model_id)
    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        task.status = "generating"
        old = TaskRetrievalCheckpoint(
            task_id=task_id, attempt=1, status="confirmed",
            resume_claim_token="old-token", resume_claimed_at=datetime.now() - timedelta(minutes=10),
        )
        latest = TaskRetrievalCheckpoint(
            task_id=task_id, attempt=2, status="confirmed",
            resume_claim_token="current-token", resume_claimed_at=datetime.now(),
        )
        session.add(old)
        session.add(latest)
        session.commit()
    started: list[tuple] = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.args = kwargs["args"]

        def start(self):
            started.append(self.args)

    monkeypatch.setattr(task_jobs.threading, "Thread", FakeThread)
    assert task_jobs.recover_generation_jobs() == []
    assert started == []


def test_heartbeat_keeps_running_lease_and_old_finish_cannot_overwrite_new_owner(tmp_app_data):
    import app.services.task_jobs as task_jobs
    from datetime import datetime, timedelta
    from threading import Event

    client = TestClient(create_app())
    model_id = _model(client)
    task_id = _task(client, model_id)
    with Session(get_engine()) as session:
        checkpoint = TaskRetrievalCheckpoint(
            task_id=task_id, attempt=1, status="confirmed", resume_claim_token="token-a",
            resume_claimed_at=task_jobs._utcnow() - timedelta(minutes=10), resume_status="running",
        )
        session.add(checkpoint)
        session.commit()
        checkpoint_id = int(checkpoint.id)
        original = checkpoint.resume_claimed_at
    class OneTick:
        def __init__(self):
            self.calls = 0

        def wait(self, _interval):
            self.calls += 1
            return self.calls > 1

    tick = OneTick()
    task_jobs._heartbeat_resume_claim(checkpoint_id, "token-a", tick, interval=0)
    with Session(get_engine()) as session:
        checkpoint = session.get(TaskRetrievalCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert checkpoint.resume_claimed_at > original
        checkpoint.resume_claim_token = "token-b"
        checkpoint.resume_status = "running"
        session.add(checkpoint)
        session.commit()
    task_jobs._finish_resume_claim(
        task_jobs._JobClaim(True, checkpoint_id=checkpoint_id, claim_token="token-a"),
        "failed",
    )
    with Session(get_engine()) as session:
        checkpoint = session.get(TaskRetrievalCheckpoint, checkpoint_id)
        assert checkpoint is not None
        assert checkpoint.resume_claim_token == "token-b"
        assert checkpoint.resume_status == "running"
