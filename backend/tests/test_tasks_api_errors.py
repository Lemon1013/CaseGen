import json

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import get_engine
from app.main import create_app
from app.models.entities import (
    CaseDraft,
    GenerationTask,
    Project,
    Requirement,
    TestCase as _TestCaseRow,
    TestCaseOperationLog as _TestCaseOperationLogRow,
)


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


def _seed_generated_task(session, requirement_id, content_md, version=1, project_id=None):
    task = GenerationTask(
        requirement_id=requirement_id,
        project_id=project_id,
        status="generated",
    )
    session.add(task)
    session.flush()
    draft = CaseDraft(task_id=task.id, version=version, content_md=content_md)
    session.add(draft)
    session.commit()
    return int(task.id), int(draft.id)


def test_finalize_conflicts_preview_and_overwrite(tmp_app_data):
    client = TestClient(create_app())
    with Session(get_engine()) as session:
        requirement = Requirement(title="需求", description="描述")
        session.add(requirement)
        session.flush()
        requirement_id = int(requirement.id)
        first_task_id, first_draft_id = _seed_generated_task(
            session,
            requirement_id,
            "## TC-001 - 登录\n旧内容\n- 优先级：P1",
        )
        second_task_id, second_draft_id = _seed_generated_task(
            session,
            requirement_id,
            "## TC-001 - 再次登录\n新内容\n- 优先级：P0",
        )

    assert client.post(f"/api/tasks/{first_task_id}/finalize").status_code == 200
    with Session(get_engine()) as session:
        existing = session.exec(
            select(_TestCaseRow).where(_TestCaseRow.requirement_id == requirement_id)
        ).one()
        existing_id = int(existing.id)
        original_length = len(existing.content_md)

    preview = client.get(f"/api/tasks/{second_task_id}/finalize-conflicts")
    assert preview.status_code == 200
    assert preview.json() == {"conflicts": [{
        "case_key": "TC-001",
        "existing_case_id": existing_id,
        "existing_title": "登录",
        "existing_status": "active",
        "new_title": "再次登录",
    }]}
    explicit = client.get(
        f"/api/tasks/{second_task_id}/finalize-conflicts?draft_id={second_draft_id}"
    )
    assert explicit.status_code == 200
    assert explicit.json() == preview.json()

    with Session(get_engine()) as session:
        untouched = session.get(_TestCaseRow, existing_id)
        assert untouched.title == "登录"
        assert untouched.revision == 1

    rejected = client.post(f"/api/tasks/{second_task_id}/finalize")
    assert rejected.status_code == 409
    assert "TC-001" in rejected.json()["detail"]

    accepted = client.post(
        f"/api/tasks/{second_task_id}/finalize",
        json={"draft_id": second_draft_id, "overwrite_existing": True},
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "finalized"

    with Session(get_engine()) as session:
        updated = session.get(_TestCaseRow, existing_id)
        assert updated is not None
        assert int(updated.id) == existing_id
        assert updated.title == "再次登录"
        assert updated.content_md.startswith("## TC-001 - 再次登录")
        assert updated.priority == "P0"
        assert updated.revision == 2
        assert updated.source_task_id == second_task_id
        assert updated.source_draft_id == second_draft_id
        logs = session.exec(
            select(_TestCaseOperationLogRow)
            .where(_TestCaseOperationLogRow.test_case_id == existing_id)
            .order_by(_TestCaseOperationLogRow.id.asc())
        ).all()
        update_logs = [row for row in logs if row.operation == "update"]
        assert len(update_logs) == 1
        assert update_logs[0].reason == "终版更新已有用例"
        assert update_logs[0].operator == "system"
        assert update_logs[0].diff_text == ""
        assert update_logs[0].before_length == original_length

    events = client.get(f"/api/tasks/{second_task_id}/events").json()
    finalize_events = [row for row in events if row["step"] == "finalize"]
    assert len(finalize_events) == 1
    assert json.loads(finalize_events[0]["detail_json"])["imported_case_count"] == 1


def test_finalize_conflicts_preview_error_codes(tmp_app_data):
    client = TestClient(create_app())
    with Session(get_engine()) as session:
        requirement = Requirement(title="需求", description="描述")
        session.add(requirement)
        session.flush()
        requirement_id = int(requirement.id)
        first_task_id, first_draft_id = _seed_generated_task(
            session,
            requirement_id,
            "## TC-001 - 登录\n内容",
        )
        second_task_id, _ = _seed_generated_task(
            session,
            requirement_id,
            "## TC-002 - 退出\n内容",
        )
        no_draft = GenerationTask(requirement_id=requirement_id, status="generated")
        session.add(no_draft)
        session.commit()
        no_draft_id = int(no_draft.id)

    assert client.get(f"/api/tasks/{first_task_id}/finalize-conflicts").json() == {
        "conflicts": []
    }

    missing_task = client.get("/api/tasks/999999/finalize-conflicts")
    assert missing_task.status_code == 404

    missing_draft = client.get(
        f"/api/tasks/{second_task_id}/finalize-conflicts?draft_id=999999"
    )
    assert missing_draft.status_code == 400

    foreign_draft = client.get(
        f"/api/tasks/{second_task_id}/finalize-conflicts?draft_id={first_draft_id}"
    )
    assert foreign_draft.status_code == 400

    no_draft_response = client.get(f"/api/tasks/{no_draft_id}/finalize-conflicts")
    assert no_draft_response.status_code == 400


def test_finalize_overwrite_logs_authenticated_operator(tmp_app_data, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    client = TestClient(create_app())
    origin = {"Origin": "http://testserver"}
    setup = client.post(
        "/api/auth/setup",
        headers=origin,
        json={
            "username": "admin",
            "display_name": "Admin",
            "password": "correct-horse-battery-staple",
        },
    )
    assert setup.status_code == 200
    with Session(get_engine()) as session:
        project = session.exec(select(Project)).first()
        assert project is not None
        project_id = int(project.id)
    client.params = {"project_id": project_id}

    with Session(get_engine()) as session:
        requirement = Requirement(
            project_id=project_id,
            title="需求",
            description="描述",
        )
        session.add(requirement)
        session.flush()
        requirement_id = int(requirement.id)
        first_task_id, first_draft_id = _seed_generated_task(
            session,
            requirement_id,
            "## TC-001 - 登录\n旧内容",
            project_id=project_id,
        )
        second_task_id, second_draft_id = _seed_generated_task(
            session,
            requirement_id,
            "## TC-001 - 再次登录\n新内容",
            project_id=project_id,
        )

    assert client.post(
        f"/api/tasks/{first_task_id}/finalize",
        headers=origin,
    ).status_code == 200
    accepted = client.post(
        f"/api/tasks/{second_task_id}/finalize",
        headers=origin,
        json={"draft_id": second_draft_id, "overwrite_existing": True},
    )
    assert accepted.status_code == 200

    with Session(get_engine()) as session:
        case = session.exec(
            select(_TestCaseRow).where(_TestCaseRow.requirement_id == requirement_id)
        ).one()
        logs = session.exec(
            select(_TestCaseOperationLogRow)
            .where(_TestCaseOperationLogRow.test_case_id == int(case.id))
            .order_by(_TestCaseOperationLogRow.id.asc())
        ).all()
        update_logs = [row for row in logs if row.operation == "update"]
        assert len(update_logs) == 1
        assert update_logs[0].operator == "admin"
