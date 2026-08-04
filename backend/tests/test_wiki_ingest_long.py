"""E2E: long document ingest uses multi-window wiki analyze."""

import json

from fastapi.testclient import TestClient

from app.api import documents as documents_api
from app.main import create_app


def _build_long_content() -> bytes:
    """Text long enough for multi-window with small test budgets.

    single_pass=400, window target becomes max(200, 180)=200.
    """
    head = ("章节前部说明。\n\n" * 25) + ("填充。" * 80)
    tail = "\n\n尾部条款：TAIL_INGEST_MARKER_42 仅出现在文末。\n"
    text = head + tail
    # Ensure multi-window path is taken under patched budgets.
    while len(text) <= 400:
        text = ("章节前部说明。\n\n" * 10) + text
    return text.encode("utf-8")


def _fake_chat(messages):
    system = (messages[0].get("content") or "") if messages else ""
    user = (messages[-1].get("content") or "") if messages else ""
    is_analyze = (
        "summary_title" in system
        or "key_rules" in system
        or "仅 JSON" in system
        or "分窗" in system
    )
    if is_analyze:
        if "TAIL_INGEST_MARKER_42" in user:
            rules = ["TAIL_INGEST_MARKER_42"]
        else:
            rules = ["head_only"]
        return json.dumps(
            {
                "summary_title": "长文档分窗分析",
                "key_rules": rules,
                "api_points": [],
                "test_hints": ["覆盖文末条款"],
                "entities": ["长文档"],
                "suggested_page_types": ["source_summary", "business"],
                "digest_update": "含前部与尾部要点",
            },
            ensure_ascii=False,
        )
    return """---
title: 长文档规则摘要
type: source_summary
sources: ["raw/sources/long.md"]
tags: ["长文档"]
---
长文档分窗分析后的摘要页。
---
title: 尾部条款业务
type: business
sources: ["raw/sources/long.md"]
tags: ["尾部"]
---
TAIL_INGEST_MARKER_42 相关业务规则。
"""


def test_long_document_ingest_multi_window(tmp_app_data, monkeypatch):
    import app.config as cfg
    import app.services.wiki_long_analyze as la

    # Config is read at import time and used as module attributes — patch both.
    monkeypatch.setattr(cfg, "WIKI_ANALYZE_SINGLE_PASS_CHARS", 400)
    monkeypatch.setattr(cfg, "WIKI_ANALYZE_WINDOW_CHARS", 180)
    monkeypatch.setattr(cfg, "WIKI_ANALYZE_WINDOW_OVERLAP", 30)
    monkeypatch.setattr(la.config, "WIKI_ANALYZE_SINGLE_PASS_CHARS", 400)
    monkeypatch.setattr(la.config, "WIKI_ANALYZE_WINDOW_CHARS", 180)
    monkeypatch.setattr(la.config, "WIKI_ANALYZE_WINDOW_OVERLAP", 30)

    monkeypatch.setattr(documents_api, "_INGEST_CHAT_FN", _fake_chat)
    client = TestClient(create_app())

    content = _build_long_content()
    text = content.decode("utf-8")
    assert len(text) > 400
    assert "TAIL_INGEST_MARKER_42" in text

    up = client.post(
        "/api/documents",
        files={"file": ("long.md", content, "text/markdown")},
    )
    assert up.status_code == 200
    doc_id = up.json()["id"]

    ing = client.post(f"/api/documents/{doc_id}/ingest")
    assert ing.status_code == 200
    job = ing.json()
    assert job["status"] == "success", job
    assert job["document_id"] == doc_id

    log = json.loads(job["step_log_json"] or "[]")
    steps = [e["step"] for e in log]
    assert "wiki_analyze_plan" in steps, steps
    assert "wiki_analyze_window" in steps, steps
    assert "wiki_analyze_consolidate" in steps, steps

    doc = client.get(f"/api/documents/{doc_id}").json()
    assert doc["status"] == "ready"

    pages = client.get("/api/wiki/pages").json()
    assert len(pages) >= 1
