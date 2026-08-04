from fastapi.testclient import TestClient
from sqlmodel import Session

from app.api import documents as documents_api
from app.db import get_engine
from app.main import create_app
from app.models.entities import Document, IngestJob
from app.services.llm import LLMError
from app.services.wiki_jobs import recover_ingest_jobs, set_scheduler


class RecordingScheduler:
    def __init__(self):
        self.scheduled: list[int] = []
        self.active: set[int] = set()

    def schedule(self, job_id: int) -> bool:
        if job_id in self.scheduled:
            return False
        self.scheduled.append(job_id)
        return True

    def is_pending_or_active(self, job_id: int) -> bool:
        return job_id in self.active


def _upload(client: TestClient, name: str = "rules.md") -> int:
    response = client.post(
        "/api/documents",
        files={"file": (name, b"# Rule\nBalance must be sufficient.", "text/markdown")},
    )
    assert response.status_code == 200
    return response.json()["id"]


def _fake_chat(messages):
    system = messages[0]["content"]
    if "JSON" in system or "分析" in system:
        return (
            '{"summary_title":"余额规则","key_rules":["余额必须充足"],'
            '"api_points":[],"test_hints":["余额不足"],"entities":["余额"],'
            '"suggested_page_types":["source_summary","business"]}'
        )
    return """---
title: 余额规则
type: source_summary
sources: []
tags: [余额]
---
# 余额规则
余额必须充足。
"""


def test_ingest_returns_queued_and_deduplicates_active_job(tmp_app_data, monkeypatch):
    scheduler = RecordingScheduler()
    previous = set_scheduler(scheduler)
    try:
        monkeypatch.setattr(documents_api, "_INGEST_CHAT_FN", None)
        client = TestClient(create_app())
        document_id = _upload(client)

        first = client.post(f"/api/documents/{document_id}/ingest")
        second = client.post(f"/api/documents/{document_id}/ingest")

        assert first.status_code == 200
        assert first.json()["status"] == "queued"
        assert first.json()["stage"] == "queued"
        assert second.json()["id"] == first.json()["id"]
        assert scheduler.scheduled == [first.json()["id"]]
    finally:
        set_scheduler(previous)


def test_queued_and_running_cancel_semantics(tmp_app_data, monkeypatch):
    scheduler = RecordingScheduler()
    previous = set_scheduler(scheduler)
    try:
        monkeypatch.setattr(documents_api, "_INGEST_CHAT_FN", None)
        client = TestClient(create_app())
        queued_document = _upload(client, "queued.md")
        queued = client.post(f"/api/documents/{queued_document}/ingest").json()
        cancelled = client.post(f"/api/ingest-jobs/{queued['id']}/cancel").json()
        assert cancelled["status"] == "cancelled"
        assert cancelled["stage"] == "cancelled"
        assert cancelled["cancel_requested"] is True

        running_document = _upload(client, "running.md")
        running = client.post(f"/api/documents/{running_document}/ingest").json()
        with Session(get_engine()) as session:
            row = session.get(IngestJob, running["id"])
            row.status = "running"
            row.stage = "analyzing"
            session.add(row)
            session.commit()
        requested = client.post(f"/api/ingest-jobs/{running['id']}/cancel").json()
        assert requested["status"] == "running"
        assert requested["cancel_requested"] is True
    finally:
        set_scheduler(previous)


def test_sync_injected_job_completes_and_fingerprint_skips_unmodified(
    tmp_app_data, monkeypatch
):
    monkeypatch.setattr(documents_api, "_INGEST_CHAT_FN", _fake_chat)
    client = TestClient(create_app())
    document_id = _upload(client)

    first = client.post(f"/api/documents/{document_id}/ingest").json()
    assert first["status"] == "success"
    assert first["stage"] == "ready"
    assert first["progress"] == 100
    assert first["model_ref"] == "injected"

    skipped = client.post(f"/api/documents/{document_id}/ingest").json()
    assert skipped["id"] == first["id"]
    forced = client.post(f"/api/documents/{document_id}/ingest?force=true").json()
    assert forced["id"] != first["id"]
    assert forced["status"] == "success"


def test_recover_resets_running_and_schedules_durable_jobs(tmp_app_data):
    client = TestClient(create_app())
    del client
    with Session(get_engine()) as session:
        document = Document(
            filename="recover.md",
            stored_path="raw/sources/recover.md",
            content_type="text/markdown",
            sha256="b" * 64,
            status="ingesting",
        )
        session.add(document)
        session.flush()
        queued = IngestJob(document_id=document.id, status="queued", stage="queued")
        running = IngestJob(document_id=document.id, status="running", stage="writing")
        session.add(queued)
        session.add(running)
        session.commit()
        queued_id, running_id = queued.id, running.id

    scheduler = RecordingScheduler()
    previous = set_scheduler(scheduler)
    try:
        recovered = recover_ingest_jobs()
        assert recovered == [queued_id, running_id]
        assert scheduler.scheduled == [queued_id, running_id]
        with Session(get_engine()) as session:
            running = session.get(IngestJob, running_id)
            assert running.status == "queued"
            assert running.stage == "queued"
            assert "recovered" in running.step_log_json
    finally:
        set_scheduler(previous)


def test_failed_job_allows_a_new_attempt(tmp_app_data, monkeypatch):
    def fail_chat(messages):
        raise LLMError("injected failure")

    monkeypatch.setattr(documents_api, "_INGEST_CHAT_FN", fail_chat)
    client = TestClient(create_app())
    document_id = _upload(client)
    first = client.post(f"/api/documents/{document_id}/ingest").json()
    second = client.post(f"/api/documents/{document_id}/ingest").json()
    assert first["status"] == "failed"
    assert second["status"] == "failed"
    assert second["id"] != first["id"]
