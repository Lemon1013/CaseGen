"""Task 12 release regressions for overlapping Wiki sources and old citations."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api import documents as documents_api
from app.db import get_engine, init_db
from app.main import create_app
from app.models.entities import (
    GenerationTask,
    Requirement,
    TaskCitation,
    WikiPageRevision,
    WikiPageRow,
    WikiReviewItem,
)


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
V1_FIXTURE = FIXTURES / "overlap_balance_rules_v1.md"
V2_FIXTURE = FIXTURES / "overlap_balance_rules_v2.md"
SAMPLE_FIXTURE = FIXTURES / "sample_balance_rules.md"


def _overlap_plan(*, version: int) -> dict:
    """Return the deterministic Step A response used by the fake chat hook."""

    operation = "create" if version == 1 else "update"
    return {
        "source_summary": {
            "title": f"余额交易规则版本{version}",
            "summary": "现货限价买单需要先校验可用余额。",
        },
        "claims": [
            {
                "claim_id": "balance-check",
                "statement": "现货限价买单提交时比较可用余额与订单所需金额。",
                "clauses": ["4.1"],
                "source_anchors": [{"window_index": 1}],
            }
        ],
        "entities": ["余额", "限价买单"],
        "related_pages": [],
        "contradictions": [],
        "page_operations": [
            {
                "op": operation,
                "page_key": "rule.order.balance",
                "page_type": "rule",
                "reason": f"余额规则增量版本{version}",
                "claim_ids": ["balance-check"],
                "source_anchors": [{"window_index": 1}],
            }
        ],
        "review_items": [],
    }


def _overlap_candidate(*, version: int) -> dict:
    operation = "create" if version == 1 else "update"
    amount = 100 if version == 1 else 120
    return {
        "operation": operation,
        "page_key": "rule.order.balance",
        "title": "余额校验规则",
        "type": "rule",
        "aliases": ["余额不足校验"],
        "tags": ["余额", "限价买单"],
        "sources": [],
        "body": (
            "# 余额校验规则\n\n"
            f"4.1 可用余额为 {amount} 元、订单所需金额为 600 元时，应拒绝下单，返回余额不足。"
        ),
    }


def _fake_overlap_chat(messages: list[dict[str, str]]) -> str:
    """Deterministic replacement for both Step A and Step B chat calls."""

    user = messages[-1].get("content", "") if messages else ""
    version = 2 if "版本二" in user or "_v2" in user else 1
    if "# Step A 分析结果" in user:
        return json.dumps(
            {"pages": [_overlap_candidate(version=version)]},
            ensure_ascii=False,
        )
    return json.dumps(_overlap_plan(version=version), ensure_ascii=False)


def _ingest_fixture(client: TestClient, fixture: Path) -> dict:
    response = client.post(
        "/api/documents",
        files={
            "file": (
                fixture.name,
                fixture.read_bytes(),
                "text/markdown",
            )
        },
    )
    assert response.status_code == 200, response.text
    document_id = response.json()["id"]
    result = client.post(f"/api/documents/{document_id}/ingest")
    assert result.status_code == 200, result.text
    return result.json()


def test_overlapping_rule_documents_update_or_review_without_duplicate_pages(
    tmp_app_data,
    monkeypatch,
):
    """Two overlapping sources keep one stable page identity across ingest."""

    monkeypatch.setattr(documents_api, "_INGEST_CHAT_FN", _fake_overlap_chat)
    client = TestClient(create_app())

    first_job = _ingest_fixture(client, V1_FIXTURE)
    assert first_job["status"] == "success", first_job

    with Session(get_engine()) as session:
        first_pages = session.exec(
            select(WikiPageRow).where(WikiPageRow.page_key == "rule.order.balance")
        ).all()
        assert len(first_pages) == 1
        first_page_id = first_pages[0].id
        assert first_pages[0].revision == 1

    second_job = _ingest_fixture(client, V2_FIXTURE)
    assert second_job["status"] == "success", second_job

    plan = json.loads(second_job["plan_json"])
    assert plan["step_a_plan"]["page_operations"] == [
        {
            **plan["step_a_plan"]["page_operations"][0],
            "op": "update",
        }
    ]

    step_log = json.loads(second_job["step_log_json"])
    apply_step = next(item for item in step_log if item["step"] == "wiki_apply")
    assert apply_step["applied_page_keys"] == ["source.document.2"]
    assert "rule.order.balance" not in apply_step["applied_page_keys"]
    assert apply_step["review_item_ids"]

    with Session(get_engine()) as session:
        pages = session.exec(
            select(WikiPageRow).where(WikiPageRow.page_key == "rule.order.balance")
        ).all()
        assert len(pages) == 1
        assert pages[0].id == first_page_id
        assert pages[0].revision == 1

        review = session.get(WikiReviewItem, apply_step["review_item_ids"][0])
        assert review is not None
        assert review.page_id == first_page_id
        payload = json.loads(review.payload_json)
        assert payload["operation"] == "update"
        assert "numeric_change" in payload["risk_flags"]

    # The pending candidate can still be approved as a revision; approval must
    # update the stable page rather than create a second row.
    approved = client.post(
        f"/api/wiki/reviews/{apply_step['review_item_ids'][0]}/approve",
        json={"reviewed_by": "release-regression", "reason": "确认版本二"},
    )
    assert approved.status_code == 200, approved.text

    with Session(get_engine()) as session:
        pages = session.exec(
            select(WikiPageRow).where(WikiPageRow.page_key == "rule.order.balance")
        ).all()
        assert len(pages) == 1
        assert pages[0].revision == 2
        revisions = session.exec(
            select(WikiPageRevision)
            .where(WikiPageRevision.page_id == pages[0].id)
            .order_by(WikiPageRevision.revision)
        ).all()
        assert [revision.operation for revision in revisions] == ["create", "update"]


def test_sample_balance_rules_release_fixture_ingests(tmp_app_data, monkeypatch):
    """The documented demo fixture remains usable by the governed ingest path."""

    monkeypatch.setattr(documents_api, "_INGEST_CHAT_FN", _fake_overlap_chat)
    client = TestClient(create_app())
    job = _ingest_fixture(client, SAMPLE_FIXTURE)
    assert job["status"] == "success", job
    with Session(get_engine()) as session:
        pages = session.exec(
            select(WikiPageRow).where(WikiPageRow.page_key == "rule.order.balance")
        ).all()
        assert len(pages) == 1
        assert pages[0].revision == 1


def _seed_legacy_citation() -> int:
    init_db()
    with Session(get_engine()) as session:
        requirement = Requirement(
            title="旧引用兼容",
            description="旧 TaskCitation 只有页面 ID 和路径。",
        )
        session.add(requirement)
        session.flush()
        task = GenerationTask(requirement_id=requirement.id, status="generated")
        session.add(task)
        session.flush()
        session.add(
            TaskCitation(
                task_id=task.id,
                wiki_page_id=987654,
                title="旧余额规则",
                path="pages/legacy-balance.md",
                score=1.0,
                snippet="旧引用正文",
            )
        )
        session.commit()
        return int(task.id)


def test_legacy_task_citation_api_remains_open(tmp_app_data):
    """Rows written before source chunks were added remain listable by the API."""

    client = TestClient(create_app())
    task_id = _seed_legacy_citation()

    response = client.get(f"/api/tasks/{task_id}/citations")
    assert response.status_code == 200, response.text
    citations = response.json()
    assert len(citations) == 1
    assert citations[0]["wiki_page_id"] == 987654
    assert citations[0]["path"] == "pages/legacy-balance.md"
    assert citations[0]["source_chunk_id"] is None


def test_unresolved_legacy_citation_exposes_explicit_legacy_marker(tmp_app_data):
    """Define the release contract for old citations with no resolvable page."""

    client = TestClient(create_app())
    task_id = _seed_legacy_citation()
    response = client.get(f"/api/tasks/{task_id}/citations")
    assert response.status_code == 200, response.text
    citation = response.json()[0]

    # Contract reserved for the mainline API: the client must be able to
    # distinguish an intentionally unresolved legacy reference from a normal
    # Wiki citation without guessing from path/id fields.
    assert citation["legacy"] is True
    assert citation["available"] is False
    assert "历史摘录" in citation.get("legacy_reason", "")
