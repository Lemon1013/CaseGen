from fastapi.testclient import TestClient

from app.main import create_app


def test_wiki_index_empty(tmp_app_data):
    client = TestClient(create_app())
    r = client.get("/api/wiki/index")
    assert r.status_code == 200
    body = r.json()
    assert "content" in body
    assert "Wiki Index" in body["content"] or body["content"] is not None


def test_wiki_pages_empty_list(tmp_app_data):
    client = TestClient(create_app())
    r = client.get("/api/wiki/pages")
    assert r.status_code == 200
    assert r.json() == []


def test_retrieve_empty_corpus(tmp_app_data):
    client = TestClient(create_app())
    r = client.post("/api/wiki/retrieve", json={"query": "余额", "top_k": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "余额"
    assert body["hits"] == []


def test_retrieve_validation(tmp_app_data):
    client = TestClient(create_app())
    r = client.post("/api/wiki/retrieve", json={})
    assert r.status_code == 422
