from fastapi.testclient import TestClient

from app.main import create_app


def test_create_model_and_list_masks_key(tmp_app_data):
    client = TestClient(create_app())
    r = client.post(
        "/api/models",
        json={
            "name": "local",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-abcdefghijkl",
            "model_name": "gpt-test",
            "is_default": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "local"
    assert body["api_key"] == "***ijkl"
    assert "sk-abcd" not in body["api_key"]

    listed = client.get("/api/models").json()
    assert len(listed) >= 1
    match = next(item for item in listed if item["id"] == body["id"])
    assert match["api_key"] == "***ijkl"

    detail = client.get(f"/api/models/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["api_key"] == "***ijkl"


def test_ping_model_success(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    created = client.post(
        "/api/models",
        json={
            "name": "ping-model",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test-key",
            "model_name": "gpt-test",
        },
    ).json()

    def fake_chat_completion(**kwargs):
        assert kwargs["messages"] == [{"role": "user", "content": "ping"}]
        assert kwargs["api_key"] == "sk-test-key"
        assert kwargs["stream"] is True
        assert kwargs["max_tokens"] == 32
        assert kwargs["max_retries"] == 1
        assert kwargs["thinking"] is None
        return "pong", {"prompt_tokens": 1}

    monkeypatch.setattr("app.api.models_cfg.chat_completion", fake_chat_completion)
    r = client.post(f"/api/models/{created['id']}/ping")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["content"] == "pong"


def test_ping_model_failure(tmp_app_data, monkeypatch):
    from app.services.llm import LLMError

    client = TestClient(create_app())
    created = client.post(
        "/api/models",
        json={
            "name": "bad-model",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-bad",
            "model_name": "gpt-test",
        },
    ).json()

    def fake_chat_completion(**kwargs):
        raise LLMError("connection refused")

    monkeypatch.setattr("app.api.models_cfg.chat_completion", fake_chat_completion)
    r = client.post(f"/api/models/{created['id']}/ping")
    assert r.status_code == 400
    assert "connection refused" in r.json()["detail"]
