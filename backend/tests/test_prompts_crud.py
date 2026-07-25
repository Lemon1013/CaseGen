from fastapi.testclient import TestClient

from app.main import create_app


def test_get_update_prompt(tmp_app_data):
    client = TestClient(create_app())
    created = client.post(
        "/api/prompts",
        json={
            "name": "custom-gen",
            "type": "generate",
            "content": "旧内容",
            "is_active": False,
        },
    )
    assert created.status_code == 200
    pid = created.json()["id"]
    assert created.json()["version"] >= 1

    got = client.get(f"/api/prompts/{pid}")
    assert got.status_code == 200
    assert got.json()["content"] == "旧内容"
    assert got.json()["is_active"] is False

    updated = client.put(
        f"/api/prompts/{pid}",
        json={"content": "新内容", "is_active": True, "name": "custom-gen-v2"},
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["content"] == "新内容"
    assert body["name"] == "custom-gen-v2"
    assert body["is_active"] is True

    # Only one active generate prompt
    actives = [
        p
        for p in client.get("/api/prompts", params={"type": "generate"}).json()
        if p["is_active"]
    ]
    assert len(actives) == 1
    assert actives[0]["id"] == pid


def test_list_filter_by_type(tmp_app_data):
    client = TestClient(create_app())
    items = client.get("/api/prompts", params={"type": "review"}).json()
    assert items
    assert all(p["type"] == "review" for p in items)


def test_update_missing_prompt_404(tmp_app_data):
    client = TestClient(create_app())
    assert client.put("/api/prompts/99999", json={"content": "x"}).status_code == 404
