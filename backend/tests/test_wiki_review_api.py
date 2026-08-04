from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import get_engine, init_db
from app.main import create_app
from app.models.entities import Document, IngestJob, WikiPageRevision, WikiReviewItem
from app.services.wiki_repository import WikiRepository
from app.services.wiki_schema import WikiFrontmatter, WikiPage, WikiSource


def _seed_page_and_job() -> tuple[int, int, int]:
    init_db()
    with Session(get_engine()) as session:
        document = Document(
            filename="rules.md",
            stored_path="raw/sources/rules.md",
            content_type="text/markdown",
            sha256="review-api-rules",
            status="ready",
        )
        session.add(document)
        session.flush()
        job = IngestJob(document_id=int(document.id), status="completed")
        session.add(job)
        session.flush()
        page = WikiRepository(session).create(
            WikiPage(
                frontmatter=WikiFrontmatter(
                    page_key="rule.order.limit",
                    title="申报限额",
                    type="rule",
                    aliases=["旧别名"],
                    sources=[WikiSource(document_id=int(document.id), clauses=["3.1"])],
                ),
                body="单笔申报上限为100万股。",
            )
        )
        return int(document.id), int(job.id), int(page.id)


def _review(
    *,
    page_id: int | None,
    job_id: int | None,
    kind: str = "numeric_change",
    status: str = "pending",
    candidate: dict | None = None,
    payload: dict | None = None,
    content: str | None = "单笔申报上限为200万股。",
) -> int:
    if candidate is None:
        candidate = {
            "page_key": "rule.order.limit",
            "title": "申报限额",
            "type": "rule",
            "aliases": ["新别名"],
            "sources": [{"document_id": 1, "clauses": ["3.1"]}],
        }
    payload = payload or {
        "operation": "update",
        "page_key": candidate.get("page_key", "rule.order.limit"),
        "risk_flags": [kind],
    }
    with Session(get_engine()) as session:
        item = WikiReviewItem(
            page_id=page_id,
            job_id=job_id,
            kind=kind,
            status=status,
            reason="关键数值发生变化",
            candidate_frontmatter_json=json.dumps(candidate, ensure_ascii=False),
            candidate_content_md=content,
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        return int(item.id)


def _client(tmp_app_data) -> TestClient:
    init_db()
    return TestClient(create_app())


def test_review_list_filters_and_detail_diff(tmp_app_data):
    _document_id, job_id, page_id = _seed_page_and_job()
    review_id = _review(page_id=page_id, job_id=job_id)
    _review(page_id=page_id, job_id=job_id, kind="conflict", status="rejected")
    client = _client(tmp_app_data)

    response = client.get(
        "/api/wiki/reviews",
        params={"status": "pending", "kind": "numeric_change", "page_id": page_id, "job_id": job_id},
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [review_id]

    detail = client.get(f"/api/wiki/reviews/{review_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert "100万股" in body["old_version"]["content_md"]
    assert "200万股" in body["new_candidate"]["content_md"]
    assert body["reason_detail"]["risk_flags"] == ["numeric_change"]
    assert body["payload"]["operation"] == "update"
    assert body["source_evidence"][0]["document_id"] == 1
    assert "-单笔申报上限为100万股。" in body["diff"]["unified"]


def test_approve_applies_candidate_and_reject_is_one_shot(tmp_app_data):
    _document_id, job_id, page_id = _seed_page_and_job()
    review_id = _review(page_id=page_id, job_id=job_id)
    client = _client(tmp_app_data)

    approved = client.post(f"/api/wiki/reviews/{review_id}/approve", json={"reviewed_by": "tester", "reason": "核对通过"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["diff"]["changed"] is True
    assert "100万股" in approved.json()["old_version"]["content_md"]
    with Session(get_engine()) as session:
        record = WikiRepository(session).read("rule.order.limit")
        revisions = session.exec(
            select(WikiPageRevision).where(WikiPageRevision.page_id == page_id).order_by(WikiPageRevision.revision)
        ).all()
        assert "200万股" in record.body
        assert len(revisions) == 2
        assert revisions[-1].job_id == job_id
        assert revisions[-1].reason == "核对通过"
        assert set(record.frontmatter.aliases) == {"旧别名", "新别名"}

    assert client.post(f"/api/wiki/reviews/{review_id}/reject").status_code == 409

    rejected_id = _review(page_id=page_id, job_id=job_id, kind="conflict")
    rejected = client.post(f"/api/wiki/reviews/{rejected_id}/reject", json={"reason": "保留旧规则"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert client.post(f"/api/wiki/reviews/{rejected_id}/reject").status_code == 409
    assert client.post(f"/api/wiki/reviews/{rejected_id}/approve").status_code == 409


def test_merge_and_missing_or_invalid_candidates_stay_pending(tmp_app_data):
    _document_id, job_id, page_id = _seed_page_and_job()
    merge_id = _review(
        page_id=None,
        job_id=job_id,
        kind="merge",
        candidate={"target_page_key": "rule.order.limit"},
        payload={"operation": "merge", "page_key": "rule.order.duplicate"},
    )
    missing_id = _review(page_id=page_id, job_id=job_id, candidate={}, payload={"operation": "update"}, content=None)
    invalid_id = _review(
        page_id=page_id,
        job_id=job_id,
        candidate={"page_key": "../escape", "type": "rule"},
        payload={"operation": "update", "page_key": "rule.order.limit"},
    )
    client = _client(tmp_app_data)

    assert client.post(f"/api/wiki/reviews/{merge_id}/approve").status_code == 409
    assert client.post(f"/api/wiki/reviews/{missing_id}/approve").status_code == 422
    assert client.post(f"/api/wiki/reviews/{invalid_id}/approve").status_code == 422
    with Session(get_engine()) as session:
        assert session.get(WikiReviewItem, merge_id).status == "pending"
        assert session.get(WikiReviewItem, missing_id).status == "pending"
        assert session.get(WikiReviewItem, invalid_id).status == "pending"


def test_revisions_detail_and_rollback_create_new_revision(tmp_app_data):
    _document_id, job_id, page_id = _seed_page_and_job()
    with Session(get_engine()) as session:
        repository = WikiRepository(session)
        repository.update(
            "rule.order.limit",
            WikiPage(
                frontmatter=WikiFrontmatter(
                    page_key="rule.order.limit",
                    title="申报限额",
                    type="rule",
                    sources=[WikiSource(document_id=1, clauses=["3.1"])],
                ),
                body="单笔申报上限为200万股。",
            ),
            job_id=job_id,
            reason="新版本",
        )
        first = session.exec(
            select(WikiPageRevision).where(WikiPageRevision.page_id == page_id).order_by(WikiPageRevision.revision)
        ).first()
        assert first is not None
        first_id = int(first.id)

    client = _client(tmp_app_data)
    revisions = client.get(f"/api/wiki/pages/{page_id}/revisions")
    assert revisions.status_code == 200
    assert [item["revision"] for item in revisions.json()] == [1, 2]
    revision_detail = client.get(f"/api/wiki/pages/{page_id}/revisions/{first_id}")
    assert revision_detail.status_code == 200
    assert "100万股" in revision_detail.json()["content_md"]

    rollback = client.post(
        f"/api/wiki/pages/{page_id}/rollback",
        json={"revision_id": first_id, "job_id": job_id, "reason": "恢复历史版本"},
    )
    assert rollback.status_code == 200
    assert rollback.json()["revision"] == 3
    page = client.get(f"/api/wiki/pages/{page_id}").json()
    assert "100万股" in page["content"]
    with Session(get_engine()) as session:
        rows = session.exec(
            select(WikiPageRevision).where(WikiPageRevision.page_id == page_id).order_by(WikiPageRevision.revision)
        ).all()
        assert len(rows) == 3
        assert rows[-1].operation == "rollback"
        assert rows[1].content_md != rows[-1].content_md

    assert client.post(f"/api/wiki/pages/{page_id}/rollback", json={}).status_code == 422
    assert client.post(
        f"/api/wiki/pages/{page_id}/rollback",
        json={"revision_id": first_id, "revision": 1},
    ).status_code == 422


def test_approve_create_links_review_to_created_page(tmp_app_data):
    document_id, job_id, _page_id = _seed_page_and_job()
    review_id = _review(
        page_id=None,
        job_id=job_id,
        kind="new_rule",
        candidate={
            "page_key": "rule.order.new-limit",
            "title": "新申报规则",
            "type": "rule",
            "sources": [{"document_id": document_id, "clauses": ["3.2"]}],
        },
        payload={"operation": "create", "page_key": "rule.order.new-limit"},
        content="新申报规则正文。",
    )
    client = _client(tmp_app_data)

    response = client.post(f"/api/wiki/reviews/{review_id}/approve")

    assert response.status_code == 200, response.json()
    assert response.json()["page_id"] is not None
    with Session(get_engine()) as session:
        item = session.get(WikiReviewItem, review_id)
        assert item is not None and item.page_id is not None
        assert WikiRepository(session).read("rule.order.new-limit").revision == 1
