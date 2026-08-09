"""Integration coverage for candidate recall and durable Step A plans."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import documents as documents_api
from app.db import get_engine, init_db
from app.main import create_app
from app.models.entities import (
    Document,
    WikiPageRevision,
    WikiPageRow,
    WikiPageSource,
)


def test_ingest_recalls_clause_candidate_and_persists_step_a_plan(
    tmp_app_data,
    monkeypatch,
):
    client = TestClient(create_app())
    init_db()
    with Session(get_engine()) as session:
        prior_source = Document(
            filename="prior.md",
            stored_path="raw/sources/prior.md",
            content_type="text/markdown",
            sha256="prior",
            status="ready",
        )
        session.add(prior_source)
        session.flush()
        page = WikiPageRow(
            path="rules/rule.order.auction.md",
            title="集合竞价成交规则",
            page_type="rule",
            page_key="rule.order.auction",
            tags_json='["竞价"]',
        )
        session.add(page)
        session.flush()
        session.add(
            WikiPageSource(
                page_id=int(page.id),
                document_id=int(prior_source.id),
                clauses_json='["3.5.2"]',
            )
        )
        session.commit()

    analyze_prompts: list[str] = []

    def fake_chat(messages):
        system = messages[0].get("content") or ""
        user = messages[-1].get("content") or ""
        if "source_summary" in system and "page_operations" in system:
            analyze_prompts.append(user)
            return json.dumps(
                {
                    "source_summary": {"title": "集合竞价补充", "summary": "补充成交原则"},
                    "claims": [
                        {
                            "claim_id": "claim-1",
                            "statement": "集合竞价按最大成交量确定价格",
                            "clauses": ["3.5.2"],
                            "source_anchors": [
                                {
                                    "window_index": 1,
                                    "start_char": 0,
                                    "end_char": 20,
                                    "clause_id": "3.5.2",
                                }
                            ],
                        }
                    ],
                    "entities": ["集合竞价"],
                    "related_pages": [
                        {"page_key": "rule.order.auction", "reason": "同一条款"}
                    ],
                    "contradictions": [],
                    "page_operations": [
                        {
                            "op": "update",
                            "page_key": "rule.order.auction",
                            "reason": "补充成交价格原则",
                            "source_anchors": [
                                {
                                    "window_index": 1,
                                    "start_char": 0,
                                    "end_char": 20,
                                    "clause_id": "3.5.2",
                                }
                            ],
                        }
                    ],
                    "review_items": [],
                },
                ensure_ascii=False,
            )
        return """---
title: 集合竞价补充摘要
type: source_summary
sources: ["raw/sources/new.md"]
tags: ["竞价"]
---
集合竞价按最大成交量确定价格。
"""

    monkeypatch.setattr(documents_api, "_INGEST_CHAT_FN", fake_chat)
    uploaded = client.post(
        "/api/documents",
        files={
            "file": (
                "new.md",
                "# 集合竞价\n3.5.2　集合竞价按最大成交量确定价格。".encode("utf-8"),
                "text/markdown",
            )
        },
    )
    assert uploaded.status_code == 200

    job = client.post(f"/api/documents/{uploaded.json()['id']}/ingest").json()

    assert job["status"] == "success", job
    assert analyze_prompts
    assert "rule.order.auction" in analyze_prompts[0]
    assert '"chunk_id"' in analyze_prompts[0]
    persisted = json.loads(job["plan_json"])
    assert persisted["candidate_page_keys"] == ["rule.order.auction"]
    assert persisted["step_a_plan"]["page_operations"][0]["op"] == "update"
    assert persisted["window_results"]


def test_structured_step_b_create_is_applied_with_job_revision(tmp_app_data, monkeypatch):
    client = TestClient(create_app())

    def fake_chat(messages):
        system = messages[0].get("content") or ""
        if "source_summary" in system and "page_operations" in system:
            return json.dumps(
                {
                    "source_summary": {"title": "竞价规则", "summary": "成交原则"},
                    "claims": [
                        {
                            "claim_id": "c1",
                            "statement": "集合竞价按最大成交量确定价格",
                            "clauses": ["3.5.2"],
                            "source_anchors": [
                                {
                                    "window_index": 1,
                                    "start_char": 0,
                                    "end_char": 20,
                                    "clause_id": "3.5.2",
                                }
                            ],
                        }
                    ],
                    "entities": ["集合竞价"],
                    "related_pages": [],
                    "contradictions": [],
                    "page_operations": [
                        {
                            "op": "create",
                            "page_key": "rule.order.auction-price",
                            "page_type": "rule",
                            "reason": "新增成交原则",
                            "claim_ids": ["c1"],
                            "source_anchors": [
                                {
                                    "window_index": 1,
                                    "start_char": 0,
                                    "end_char": 20,
                                    "clause_id": "3.5.2",
                                }
                            ],
                        }
                    ],
                    "review_items": [],
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "pages": [
                    {
                        "operation": "create",
                        "page_key": "rule.order.auction-price",
                        "title": "集合竞价成交价格",
                        "type": "rule",
                        "aliases": [],
                        "tags": ["竞价"],
                        "sources": [],
                        "body": "3.5.2 集合竞价按最大成交量确定成交价格。",
                        "reason": "新增成交原则",
                    },
                    {
                        "operation": "create",
                        "page_key": "source_summary.hallucinated",
                        "title": "模型额外生成的页面",
                        "type": "source",
                        "aliases": [],
                        "tags": [],
                        "sources": [],
                        "body": "这页不在 Step A 计划中。",
                        "reason": "未请求的额外页面",
                    }
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(documents_api, "_INGEST_CHAT_FN", fake_chat)
    uploaded = client.post(
        "/api/documents",
        files={
            "file": (
                "auction.md",
                "# 集合竞价\n3.5.2　集合竞价按最大成交量确定价格。".encode("utf-8"),
                "text/markdown",
            )
        },
    )
    job = client.post(f"/api/documents/{uploaded.json()['id']}/ingest").json()

    assert job["status"] == "success_with_warnings", job
    log = json.loads(job["step_log_json"])
    apply_step = next(item for item in log if item["step"] == "wiki_apply")
    assert "rule.order.auction-price" in apply_step["applied_page_keys"]
    assert apply_step["source_summary_key"].startswith("source.document.")
    assert any(item["step"] == "wiki_write_sanitized" for item in log)
    with Session(get_engine()) as session:
        revisions = session.exec(select(WikiPageRevision)).all()
        assert len(revisions) == 2
        assert all(item.job_id == job["id"] for item in revisions)
