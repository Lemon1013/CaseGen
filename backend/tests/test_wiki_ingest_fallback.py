"""Regression: wiki_write LLM 502 must still produce ready wiki via analysis fallback."""

import json

from fastapi.testclient import TestClient

from app.api import documents as documents_api
from app.main import create_app
from app.services.llm import LLMError


def test_ingest_falls_back_when_wiki_write_returns_502(tmp_app_data, monkeypatch):
    calls = {"n": 0}

    def flaky_chat(messages):
        calls["n"] += 1
        system = (messages[0].get("content") or "") if messages else ""
        # First call = analyze
        if calls["n"] == 1 or "summary_title" in system or "仅 JSON" in system:
            return json.dumps(
                {
                    "source_summary": {
                        "title": "上交所交易规则摘要",
                        "summary": "集合竞价与连续竞价规则",
                    },
                    "claims": [
                        {
                            "claim_id": "claim-1",
                            "statement": "文档包含集合竞价规则",
                            "source_anchors": [
                                {"document_id": 1, "chunk_ids": [1]}
                            ],
                        }
                    ],
                    "entities": ["集合竞价", "连续竞价"],
                    "related_pages": [],
                    "contradictions": [],
                    "page_operations": [
                        {
                            "op": "create",
                            "page_key": "rule.auction.overview",
                            "reason": "新增集合竞价规则",
                            "source_anchors": [
                                {"document_id": 1, "chunk_ids": [1]}
                            ],
                            "page_type": "rule",
                            "claim_ids": ["claim-1"],
                        }
                    ],
                    "review_items": [],
                },
                ensure_ascii=False,
            )
        # Second call = wiki_write gateway failure (the real production bug)
        raise LLMError(
            "LLM HTTP 502 (http://gpt.158918.xyz/v1/chat/completions): "
        )

    monkeypatch.setattr(documents_api, "_INGEST_CHAT_FN", flaky_chat)
    client = TestClient(create_app())

    up = client.post(
        "/api/documents",
        files={"file": ("sse.md", "# 交易规则\n集合竞价\n连续竞价".encode("utf-8"), "text/markdown")},
    )
    assert up.status_code == 200
    doc_id = up.json()["id"]

    job = client.post(f"/api/documents/{doc_id}/ingest").json()
    assert job["status"] == "success", job
    log = json.loads(job["step_log_json"])
    steps = [e["step"] for e in log]
    assert "wiki_analyze" in steps
    assert "wiki_write_fallback" in steps
    assert "wiki_write" in steps
    assert "index" in steps
    write_step = next(e for e in log if e["step"] == "wiki_write")
    assert write_step.get("mode") == "deterministic_fallback"

    doc = client.get(f"/api/documents/{doc_id}").json()
    assert doc["status"] == "ready"
    assert not doc.get("error_message")

    pages = client.get("/api/wiki/pages").json()
    assert len(pages) >= 2
    titles = " ".join(p["title"] for p in pages)
    assert "上交所" in titles or "摘要" in titles or "业务" in titles

    # Retrieve must still work after fallback pages
    ret = client.post("/api/wiki/retrieve", json={"query": "集合竞价", "top_k": 5})
    assert ret.status_code == 200
    assert len(ret.json()["hits"]) >= 1


def test_ingest_fails_when_analyze_fails(tmp_app_data, monkeypatch):
    def always_fail(messages):
        raise LLMError("LLM HTTP 502 (http://example/v1/chat/completions): ")

    monkeypatch.setattr(documents_api, "_INGEST_CHAT_FN", always_fail)
    client = TestClient(create_app())
    doc_id = client.post(
        "/api/documents",
        files={"file": ("x.md", b"# hi", "text/markdown")},
    ).json()["id"]
    job = client.post(f"/api/documents/{doc_id}/ingest").json()
    assert job["status"] == "failed"
    assert "502" in (job["error_message"] or "")
    assert client.get(f"/api/documents/{doc_id}").json()["status"] == "failed"
