from fastapi.testclient import TestClient

from app.main import create_app


def test_list_tasks_empty(tmp_app_data):
    client = TestClient(create_app())
    r = client.get("/api/tasks")
    assert r.status_code == 200
    assert r.json() == []


def test_create_task_validation(tmp_app_data):
    client = TestClient(create_app())
    # missing required fields
    r = client.post("/api/tasks", json={})
    assert r.status_code == 422


def test_task_subresources_empty_on_new_task(tmp_app_data):
    client = TestClient(create_app())
    tid = client.post(
        "/api/tasks",
        json={"title": "t", "description": "d", "focus_tags": ["x"]},
    ).json()["id"]

    assert client.get(f"/api/tasks/{tid}").json()["status"] == "draft"
    assert client.get(f"/api/tasks/{tid}/drafts").json() == []
    assert client.get(f"/api/tasks/{tid}/citations").json() == []
    assert client.get(f"/api/tasks/{tid}/events").json() == []
    assert client.get(f"/api/tasks/{tid}/reviews").json() == []
    assert client.get(f"/api/tasks/{tid}/revisions").json() == []


def test_review_before_generate_fails(tmp_app_data):
    client = TestClient(create_app())
    tid = client.post(
        "/api/tasks",
        json={"title": "t", "description": "d"},
    ).json()["id"]
    r = client.post(f"/api/tasks/{tid}/review")
    # Pipeline marks task failed with error_message (HTTP 200 envelope)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert body["error_message"]


def test_regenerate_before_generate_fails(tmp_app_data):
    client = TestClient(create_app())
    tid = client.post(
        "/api/tasks",
        json={"title": "t", "description": "d"},
    ).json()["id"]
    r = client.post(f"/api/tasks/{tid}/regenerate")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert body["error_message"]


def test_optimize_before_review_fails(tmp_app_data):
    client = TestClient(create_app())
    tid = client.post(
        "/api/tasks",
        json={"title": "t", "description": "d"},
    ).json()["id"]
    r = client.post(f"/api/tasks/{tid}/optimize-prompt")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert body["error_message"]


def test_apply_prompt_missing_revision(tmp_app_data):
    client = TestClient(create_app())
    tid = client.post(
        "/api/tasks",
        json={"title": "t", "description": "d"},
    ).json()["id"]
    r = client.post(
        f"/api/tasks/{tid}/apply-prompt",
        json={"revision_id": 99999, "mode": "task_temp"},
    )
    assert r.status_code in (400, 404)
