from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_engine
from app.main import create_app
from app.models.entities import TestCase as _TestCaseRow


def test_list_tasks_empty(tmp_app_data):
    client = TestClient(create_app())
    r = client.get("/api/tasks")
    assert r.status_code == 200
    assert r.json() == []


def test_delete_task_removes_task_and_children(tmp_app_data):
    client = TestClient(create_app())
    created = client.post(
        "/api/tasks",
        json={"title": "to-delete", "description": "will be removed", "focus_tags": ["x"]},
    )
    assert created.status_code == 200
    tid = created.json()["id"]
    assert client.get(f"/api/tasks/{tid}").status_code == 200

    deleted = client.delete(f"/api/tasks/{tid}")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True, "id": tid}

    assert client.get(f"/api/tasks/{tid}").status_code == 404
    assert all(t["id"] != tid for t in client.get("/api/tasks").json())

    missing = client.delete(f"/api/tasks/{tid}")
    assert missing.status_code == 404


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


def test_create_task_reuses_existing_requirement(tmp_app_data):
    client = TestClient(create_app())
    requirement = client.post(
        "/api/requirements",
        json={"title": "共享需求", "description": "同一需求可创建多个任务", "focus_tags": ["共享"]},
    )
    assert requirement.status_code == 200
    rid = requirement.json()["id"]
    first = client.post("/api/tasks", json={"requirement_id": rid})
    second = client.post("/api/tasks", json={"requirement_id": rid})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["requirement_id"] == rid
    assert second.json()["requirement_id"] == rid
    assert client.get("/api/requirements").json()


def test_delete_task_keeps_imported_case_and_requirement(tmp_app_data):
    client = TestClient(create_app())
    requirement = client.post(
        "/api/requirements",
        json={"title": "已入库需求", "description": "删除任务不应删除资产"},
    ).json()
    task = client.post("/api/tasks", json={"requirement_id": requirement["id"]}).json()
    with Session(get_engine()) as session:
        row = _TestCaseRow(
            requirement_id=requirement["id"],
            case_key="TC-001",
            source_case_key="TC-001",
            source_task_id=task["id"],
            source_draft_id=None,
            title="已入库",
            content_md="## TC-001\n已入库",
        )
        session.add(row)
        session.commit()
        case_id = row.id
    deleted = client.delete(f"/api/tasks/{task['id']}")
    assert deleted.status_code == 200
    with Session(get_engine()) as session:
        assert session.get(_TestCaseRow, case_id) is not None
    assert client.get(f"/api/requirements/{requirement['id']}").status_code == 200


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


def test_failed_task_can_switch_model_for_retry(tmp_app_data):
    client = TestClient(create_app())
    first = client.post(
        "/api/models",
        json={
            "name": "first",
            "base_url": "https://example.com/v1",
            "api_key": "sk-first",
            "model_name": "model-first",
        },
    ).json()
    second = client.post(
        "/api/models",
        json={
            "name": "second",
            "base_url": "https://example.com/v1",
            "api_key": "sk-second",
            "model_name": "model-second",
        },
    ).json()
    task = client.post(
        "/api/tasks",
        json={"title": "t", "description": "d", "model_id": first["id"]},
    ).json()
    task_id = task["id"]
    assert client.post(f"/api/tasks/{task_id}/review").json()["status"] == "failed"

    changed = client.patch(
        f"/api/tasks/{task_id}/model",
        json={"model_id": second["id"]},
    )
    assert changed.status_code == 200
    assert changed.json()["model_id"] == second["id"]
    events = client.get(f"/api/tasks/{task_id}/events").json()
    assert events[-1]["step"] == "model_change"

    missing = client.patch(f"/api/tasks/{task_id}/model", json={"model_id": 99999})
    assert missing.status_code == 422
