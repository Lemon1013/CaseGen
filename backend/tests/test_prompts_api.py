from fastapi.testclient import TestClient

from app.main import create_app


def test_only_one_active_prompt_per_type(tmp_app_data):
    client = TestClient(create_app())
    r1 = client.post(
        "/api/prompts",
        json={"name": "g1", "type": "generate", "content": "A", "is_active": True},
    )
    r2 = client.post(
        "/api/prompts",
        json={"name": "g2", "type": "generate", "content": "B", "is_active": True},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    items = client.get("/api/prompts", params={"type": "generate"}).json()
    actives = [p for p in items if p["is_active"]]
    assert len(actives) == 1
    assert actives[0]["content"] == "B"


def test_default_prompts_seeded(tmp_app_data):
    client = TestClient(create_app())
    items = client.get("/api/prompts").json()
    types = {p["type"] for p in items if p["is_active"]}
    assert {
        "generate",
        "review",
        "optimize",
        "wiki_analyze",
        "wiki_write",
    }.issubset(types)
