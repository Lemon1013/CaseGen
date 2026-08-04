from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_engine, init_db
from app.main import create_app
from app.models.entities import IngestJob, WikiPageRow


def test_document_preview_exposes_quality_and_text(tmp_app_data):
    client = TestClient(create_app())
    init_db()
    uploaded = client.post(
        "/api/documents",
        files={"file": ("rules.txt", "3.5.2 集合竞价按最大成交量确定价格。", "text/plain")},
    )
    assert uploaded.status_code == 200
    document_id = uploaded.json()["id"]

    response = client.get(f"/api/documents/{document_id}/preview?max_chars=500")
    assert response.status_code == 200
    data = response.json()
    assert data["document_id"] == document_id
    assert "最大成交量" in data["text"]
    assert data["quality_ok"] is True
    assert data["diagnostics"]["is_empty"] is False


def test_active_ingest_jobs_can_be_restored_by_frontend(tmp_app_data):
    client = TestClient(create_app())
    init_db()
    with Session(get_engine()) as session:
        session.add(
            IngestJob(
                document_id=88,
                status="running",
                stage="analyzing",
                progress=42,
                step_log_json='["解析完成", "正在分析窗口 2/5"]',
            )
        )
        session.add(
            IngestJob(
                document_id=99,
                status="success",
                stage="done",
                progress=100,
                step_log_json="[]",
            )
        )
        session.commit()

    response = client.get("/api/ingest-jobs?status=queued,running")
    assert response.status_code == 200
    jobs = response.json()
    assert len(jobs) == 1
    assert jobs[0]["document_id"] == 88
    assert jobs[0]["stage"] == "analyzing"
    assert jobs[0]["progress"] == 42


def test_wiki_page_list_exposes_filter_metadata(tmp_app_data):
    client = TestClient(create_app())
    init_db()
    with Session(get_engine()) as session:
        session.add(
            WikiPageRow(
                path="rules/trading.auction.md",
                title="集合竞价规则",
                page_type="rule",
                source_document_id=7,
                tags_json=json.dumps(["集合竞价"], ensure_ascii=False),
                aliases_json=json.dumps(["开盘竞价"], ensure_ascii=False),
                page_key="trading.auction",
                domain="trading",
                status="published",
                revision=3,
            )
        )
        session.commit()

    response = client.get("/api/wiki/pages")
    assert response.status_code == 200
    page = response.json()[0]
    assert page["page_key"] == "trading.auction"
    assert page["domain"] == "trading"
    assert page["status"] == "published"
    assert page["revision"] == 3
    assert page["aliases"] == ["开盘竞价"]
