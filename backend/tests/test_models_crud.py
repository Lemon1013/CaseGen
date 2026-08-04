from fastapi.testclient import TestClient

from app.main import create_app


def _create(client: TestClient, **overrides):
    body = {
        "name": "m1",
        "base_url": "http://gpt.example.com",
        "api_key": "sk-secret-key-ZZ99",
        "model_name": "grok-4.5",
        "is_default": False,
    }
    body.update(overrides)
    r = client.post("/api/models", json=body)
    assert r.status_code == 200
    return r.json()


def test_update_and_delete_model(tmp_app_data):
    client = TestClient(create_app())
    created = _create(client, name="before")
    mid = created["id"]

    updated = client.put(
        f"/api/models/{mid}",
        json={"name": "after", "model_name": "grok-new", "is_default": True},
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["name"] == "after"
    assert body["model_name"] == "grok-new"
    assert body["is_default"] is True
    assert body["api_key"].startswith("***")
    assert "secret" not in body["api_key"]

    deleted = client.delete(f"/api/models/{mid}")
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True
    assert client.get(f"/api/models/{mid}").status_code == 404


def test_update_missing_model_404(tmp_app_data):
    client = TestClient(create_app())
    r = client.put("/api/models/99999", json={"name": "x"})
    assert r.status_code == 404


def test_delete_missing_model_404(tmp_app_data):
    client = TestClient(create_app())
    r = client.delete("/api/models/99999")
    assert r.status_code == 404


def test_ping_uses_root_base_url_without_v1(tmp_app_data, monkeypatch):
    """Regression for gateway base_url without /v1 → must still work via chat_completion."""
    from app.services.llm import build_chat_completions_url

    client = TestClient(create_app())
    created = _create(client, base_url="http://gpt.158918.xyz")
    seen = {}

    def fake_chat_completion(**kwargs):
        seen["url"] = build_chat_completions_url(kwargs["base_url"])
        seen["model"] = kwargs["model"]
        return "pong", {}

    monkeypatch.setattr("app.api.models_cfg.chat_completion", fake_chat_completion)
    r = client.post(f"/api/models/{created['id']}/ping")
    assert r.status_code == 200
    assert seen["url"] == "http://gpt.158918.xyz/v1/chat/completions"
    assert seen["model"] == "grok-4.5"
