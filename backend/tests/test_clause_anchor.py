from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_engine, init_db
from app.main import create_app
from app.services.clause_index import extract_clause_ids, build_clause_index, lookup_clauses
from app.services.hybrid_retrieve import hybrid_retrieve
from app.services.source_chunks_store import replace_chunks_for_document


def test_extract_clause_ids():
    text = "见 3.5.1 与 3.5.2　集合竞价。另见 11.6。"
    ids = extract_clause_ids(text)
    assert "3.5.1" in ids
    assert "3.5.2" in ids
    assert "11.6" in ids


def test_lookup_clause_prefers_matching_chunk():
    chunks = [
        {
            "id": 1,
            "title": "2.4.2 开盘时间",
            "text": "2.4.2　9:15至9:25为开盘集合竞价时间。",
        },
        {
            "id": 2,
            "title": "3.5.2",
            "text": "3.5.1 撮合成交。\n3.5.2　集合竞价时，成交价格的确定原则为：最大成交量。",
        },
    ]
    index = build_clause_index(chunks)
    found = lookup_clauses(index, ["3.5.2"], max_chunks=3)
    assert found
    assert found[0]["id"] == 2
    assert found[0]["anchor_clause"] == "3.5.2"


def test_hybrid_retrieve_anchors_clause(tmp_app_data):
    client = TestClient(create_app())
    init_db()
    text = (
        "2.4.2　9:15至9:25为开盘集合竞价时间。\n\n"
        "第五节 成交\n"
        "3.5.1　证券竞价交易按价格优先、时间优先的原则撮合成交。\n"
        "3.5.2　集合竞价时，成交价格的确定原则为：\n"
        "（一）可实现最大成交量的价格；\n"
        "集合竞价的所有交易以同一价格成交。\n"
    )
    with Session(get_engine()) as s:
        replace_chunks_for_document(s, 77, text, chunk_chars=400, overlap_chars=40)
        s.commit()
        result = hybrid_retrieve(
            s,
            "开盘集合竞价的成交价格撮合规则 3.5.2",
            wiki_k=2,
            source_k=4,
            top_k=6,
        )
    sources = result["source_hits"]
    assert sources
    blob = " ".join(
        (h.get("title") or "") + (h.get("text") or "") + (h.get("snippet") or "")
        for h in sources
    )
    assert "3.5.2" in blob or "最大成交量" in blob or "撮合成交" in blob
    # API surface
    r = client.post(
        "/api/wiki/retrieve",
        json={"query": "开盘集合竞价 成交价格 3.5.2", "top_k": 8},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("source_hit_count", 0) >= 1
    joined = " ".join(
        (h.get("title") or "")
        + (h.get("snippet") or "")
        + " ".join(h.get("clause_ids") or [])
        for h in data["hits"]
    )
    assert "3.5" in joined
