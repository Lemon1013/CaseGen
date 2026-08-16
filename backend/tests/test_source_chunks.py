"""P0: verbatim source chunks + hybrid retrieve."""

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import get_engine, init_db
from app.main import create_app
from app.models.entities import SourceChunk
from app.services.source_chunking import chunk_text
from app.services.source_chunks_store import replace_chunks_for_document
from app.api import documents as documents_api
import time


def test_chunk_text_splits_long_doc():
    text = "\n\n".join([f"第{i}节 规则内容" + ("撤单" * 200) for i in range(1, 6)])
    chunks = chunk_text(text, chunk_chars=400, overlap_chars=50)
    assert len(chunks) >= 3
    assert all(c["text"].strip() for c in chunks)
    assert chunks[0]["start_char"] >= 0
    assert chunks[-1]["end_char"] <= len(text)
    # lossless-ish: joined length roughly covers source
    joined = "".join(c["text"] for c in chunks)
    assert "撤单" in joined
    assert "第1节" in joined or "规则内容" in joined


def test_ingest_persists_source_chunks(tmp_app_data, monkeypatch):
    def fake_chat(messages):
        system = (messages[0].get("content") or "") if messages else ""
        if "summary_title" in system or "仅 JSON" in system:
            return (
                '{"summary_title":"规则摘要","key_rules":["9:20-9:25不可撤单"],'
                '"api_points":[],"test_hints":["覆盖不可撤窗口"],'
                '"entities":["集合竞价"],"suggested_page_types":["business"]}'
            )
        return (
            "---\ntitle: 业务\ntype: business\nsources: []\ntags: [\"撤单\"]\n---\n"
            "9:20-9:25 不接受撤单\n"
        )

    monkeypatch.setattr(documents_api, "_INGEST_CHAT_FN", fake_chat)
    client = TestClient(create_app())

    body = (
        "# 交易规则\n\n"
        "开盘集合竞价时间为9:15至9:25，其中9:20至9:25不接受撤单申报。\n\n"
        "连续竞价期间未成交申报可以撤销。\n\n"
        "收盘集合竞价14:57至15:00不接受撤单。\n"
    )
    up = client.post(
        "/api/documents",
        files={"file": ("rules.md", body.encode("utf-8"), "text/markdown")},
    )
    assert up.status_code == 200
    doc_id = up.json()["id"]

    job = client.post(f"/api/documents/{doc_id}/ingest").json()
    assert job["status"] == "success"
    assert "source_chunks" in job["step_log_json"]

    chunks = client.get(f"/api/documents/{doc_id}/chunks").json()
    assert len(chunks) >= 1
    texts = " ".join(c["text"] for c in chunks)
    assert "9:20" in texts or "撤单" in texts

    ret = client.post(
        "/api/wiki/retrieve",
        json={"query": "集合竞价 撤单 9:20", "top_k": 8},
    )
    assert ret.status_code == 200
    data = ret.json()
    assert data["source_hit_count"] >= 1
    kinds = {h.get("citation_type") for h in data["hits"]}
    assert "source" in kinds


def test_generate_hybrid_citations(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    init_db()

    # seed document chunks + wiki page
    from app import config
    from app.models.entities import WikiPageRow
    import json

    config.WIKI_PAGES_DIR.mkdir(parents=True, exist_ok=True)
    rel = "pages/cancel.md"
    (config.WIKI_DIR / rel).write_text(
        "---\ntitle: 撤单规则\ntype: business\n---\n9:20-9:25 不可撤单\n",
        encoding="utf-8",
    )
    with Session(get_engine()) as s:
        s.add(
            WikiPageRow(
                path=rel,
                title="撤单规则摘要",
                page_type="business",
                tags_json=json.dumps(["撤单"], ensure_ascii=False),
            )
        )
        # fake document id 1 chunks without full document row is ok for retrieve
        replace_chunks_for_document(
            s,
            99,
            "开盘集合竞价9:15-9:25。其中9:20至9:25不接受撤单申报。连续竞价可撤。",
            chunk_chars=200,
            overlap_chars=20,
        )
        s.commit()

    mid = client.post(
        "/api/models",
        json={
            "name": "m",
            "base_url": "https://example.com/v1",
            "api_key": "k",
            "model_name": "t",
            "is_default": True,
        },
    ).json()["id"]

    def fake_chat(**kwargs):
        messages = kwargs.get("messages") or []
        joined = "\n".join(m.get("content", "") for m in messages)
        # hybrid context should include source section
        assert "原文" in joined or "Source" in joined or "[S1]" in joined or "不接受撤单" in joined
        return "# 用例：不可撤单\n- 关联知识：[1][S1]\n"

    point_payload = json.dumps(
        {
            "test_points": [
                {
                    "stable_key": "TP-001",
                    "title": "验证不可撤单窗口",
                    "verification_goal": "验证确认的 Wiki 和原文引用支持该规则",
                    "dimension": "boundary",
                    "priority": "P1",
                    "citation_ids": ["1", "S1"],
                }
            ]
        },
        ensure_ascii=False,
    )

    monkeypatch.setattr("app.api.tasks._GENERATE_CHAT_FN", fake_chat)
    monkeypatch.setattr("app.services.task_pipeline._GENERATE_CHAT_FN", fake_chat)
    monkeypatch.setattr("app.api.tasks._TEST_POINTS_CHAT_FN", lambda **kwargs: point_payload)
    monkeypatch.setattr(
        "app.services.task_pipeline._TEST_POINTS_CHAT_FN", lambda **kwargs: point_payload
    )

    tid = client.post(
        "/api/tasks",
        json={
            "title": "不可撤单窗口",
            "description": "验证9:20-9:25集合竞价不可撤单",
            "focus_tags": ["集合竞价", "撤单"],
            "model_id": mid,
        },
    ).json()["id"]

    gen = client.post(f"/api/tasks/{tid}/generate")
    assert gen.status_code == 200
    assert gen.json()["status"] == "awaiting_confirmation"
    checkpoint = client.get(f"/api/tasks/{tid}/retrieval-checkpoint")
    assert checkpoint.status_code == 200
    checkpoint_body = checkpoint.json()
    candidate_types = {item.get("citation_type") for item in checkpoint_body["candidate_citations"]}
    assert "wiki" in candidate_types
    assert "source" in candidate_types
    confirm = client.post(
        f"/api/tasks/{tid}/retrieval-checkpoint/confirm",
        json={
            "selected_citation_ids": [item["id"] for item in checkpoint_body["candidate_citations"]],
            "supplemental_text": "",
            "expected_version": checkpoint_body["version"],
            "idempotency_key": f"source-chunks-{tid}-{checkpoint_body['version']}",
        },
    )
    assert confirm.status_code == 200
    for _ in range(80):
        current = client.get(f"/api/tasks/{tid}").json()
        if current["status"] == "awaiting_test_point_confirmation":
            point_checkpoint = client.get(f"/api/tasks/{tid}/test-points").json()
            point_confirm = client.post(
                f"/api/tasks/{tid}/test-points/confirm",
                json={
                    "points": point_checkpoint["points"],
                    "expected_version": point_checkpoint["version"],
                    "idempotency_key": f"source-points-{tid}-{point_checkpoint['version']}",
                },
            )
            assert point_confirm.status_code == 200
            continue
        if current["status"] not in {"retrieving", "generating", "generating_test_points"}:
            break
        time.sleep(0.05)
    assert current["status"] == "generated"

    cites = client.get(f"/api/tasks/{tid}/citations").json()
    types = {c.get("citation_type") for c in cites}
    assert "wiki" in types or any(c.get("wiki_page_id") for c in cites)
    assert "source" in types or any(c.get("source_chunk_id") for c in cites)
    source_cites = [c for c in cites if c.get("citation_type") == "source"]
    if source_cites:
        assert source_cites[0].get("content_excerpt")
        assert "撤单" in source_cites[0]["content_excerpt"]
