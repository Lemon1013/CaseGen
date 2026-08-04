from fastapi.testclient import TestClient

from app.main import create_app
from app.api import documents as documents_api


def _fake_chat(messages):
    """Deterministic two-step LLM for ingest tests."""
    system = (messages[0].get("content") or "") if messages else ""
    # wiki_analyze prompt requires JSON schema fields; wiki_write asks for markdown pages.
    if "summary_title" in system or "key_rules" in system or "仅 JSON" in system:
        return (
            '{"summary_title":"余额","key_rules":["余额不足拒单"],'
            '"api_points":[],"test_hints":["余额0下单"],'
            '"entities":["余额"],"suggested_page_types":["business","source_summary"]}'
        )
    return """---
title: 余额规则摘要
type: source_summary
sources: ["raw/sources/rules.md"]
tags: ["余额"]
---
余额不足应拒绝下单。
---
title: 余额业务规则
type: business
sources: ["raw/sources/rules.md"]
tags: ["余额", "下单"]
---
账户余额不足时下单接口返回拒绝。
"""


def test_upload_ingest_list_retrieve(tmp_app_data, monkeypatch):
    monkeypatch.setattr(documents_api, "_INGEST_CHAT_FN", _fake_chat)
    client = TestClient(create_app())

    content = "# 余额规则\n余额不足应拒绝下单".encode("utf-8")
    files = {"file": ("rules.md", content, "text/markdown")}
    up = client.post("/api/documents", files=files)
    assert up.status_code == 200
    doc_id = up.json()["id"]

    ing = client.post(f"/api/documents/{doc_id}/ingest")
    assert ing.status_code == 200
    job = ing.json()
    assert job["status"] == "success"
    assert job["document_id"] == doc_id
    assert job["id"]

    job_get = client.get(f"/api/ingest-jobs/{job['id']}")
    assert job_get.status_code == 200
    assert job_get.json()["status"] == "success"

    doc = client.get(f"/api/documents/{doc_id}").json()
    assert doc["status"] == "ready"

    pages = client.get("/api/wiki/pages").json()
    assert len(pages) >= 1
    assert any("余额" in p["title"] for p in pages)

    page_id = pages[0]["id"]
    detail = client.get(f"/api/wiki/pages/{page_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["content"]
    assert body["title"]

    index = client.get("/api/wiki/index")
    assert index.status_code == 200
    assert "Wiki Index" in index.json()["content"] or "余额" in index.json()["content"]

    ret = client.post(
        "/api/wiki/retrieve",
        json={"query": "余额 不足", "top_k": 5},
    )
    assert ret.status_code == 200
    payload = ret.json()
    assert payload["query"] == "余额 不足"
    assert len(payload["hits"]) >= 1
    assert payload["hits"][0]["score"] > 0
    assert "余额" in payload["hits"][0]["title"] or "余额" in payload["hits"][0]["snippet"]
