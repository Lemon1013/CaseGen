"""Tests for submitter attribution on tasks/cases and case list sorting/filtering."""

from __future__ import annotations

from datetime import datetime, timedelta

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
    TestCase,
    TestCaseOperationLog,
    User,
)
from app.services.case_management import import_cases_to_requirement


def _auth_client(tmp_app_data, monkeypatch) -> TestClient:
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    return TestClient(create_app())


def _origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


def _setup_admin(client: TestClient) -> int:
    resp = client.post(
        "/api/auth/setup",
        headers=_origin(),
        json={"username": "admin", "display_name": "管理员", "password": "CaseGen-Test1!"},
    )
    assert resp.status_code == 200
    with Session(get_engine()) as session:
        project = session.exec(select(Project)).first()
        assert project is not None
        project_id = int(project.id)
    client.params = {"project_id": project_id}
    return project_id


def _create_and_login_user(
    client: TestClient,
    *,
    username: str,
    display_name: str,
    password: str = "CaseGen-User1!",
) -> int:
    created = client.post(
        "/api/users",
        headers=_origin(),
        json={
            "username": username,
            "display_name": display_name,
            "password": password,
            "role": "user",
        },
    )
    assert created.status_code == 201
    user_id = int(created.json()["id"])
    assert client.post("/api/auth/logout", headers=_origin()).status_code == 204
    assert client.post(
        "/api/auth/login",
        headers=_origin(),
        json={"username": username, "password": password},
    ).status_code == 200
    return user_id


def test_task_and_case_record_submitter_across_creation_and_finalize(tmp_app_data, monkeypatch):
    client = _auth_client(tmp_app_data, monkeypatch)
    project_id = _setup_admin(client)
    alice_id = _create_and_login_user(client, username="alice", display_name="Alice Lin")

    created_task = client.post(
        "/api/tasks",
        headers=_origin(),
        json={
            "project_id": project_id,
            "title": "集合竞价撮合",
            "description": "验证开盘集合竞价撮合逻辑",
        },
    )
    assert created_task.status_code == 200
    task_body = created_task.json()
    assert task_body["created_by_user_id"] == alice_id
    assert task_body["created_by_username"] == "alice"
    assert task_body["created_by_display_name"] == "Alice Lin"

    tasks_list = client.get("/api/tasks", headers=_origin()).json()
    assert len(tasks_list) == 1
    assert tasks_list[0]["created_by_username"] == "alice"
    assert tasks_list[0]["created_by_display_name"] == "Alice Lin"

    task_id = int(task_body["id"])
    req_id = int(task_body["requirement_id"])

    # Prepare a generated draft so we can finalize the task as Alice.
    with Session(get_engine()) as session:
        task_row = session.get(GenerationTask, task_id)
        assert task_row is not None
        task_row.status = "generated"
        session.add(task_row)
        draft = CaseDraft(
            task_id=task_id,
            version=1,
            content_md="## TC-001 - 开盘撮合成功\n- 优先级：P0\n1. 提交限价买单",
        )
        session.add(draft)
        session.commit()

    finalized = client.post(f"/api/tasks/{task_id}/finalize", headers=_origin())
    assert finalized.status_code == 200

    # Also create a direct manual case as Alice and import directly as admin.
    manual_case = client.post(
        "/api/cases",
        headers=_origin(),
        json={
            "requirement_id": req_id,
            "case_key": "TC-002",
            "title": "手工补充用例",
            "content_md": "## TC-002 - 手工补充用例\n补充内容",
            "priority": "P1",
        },
    )
    assert manual_case.status_code == 201
    assert manual_case.json()["created_by_user_id"] == alice_id
    assert manual_case.json()["created_by_username"] == "alice"
    assert manual_case.json()["created_by_display_name"] == "Alice Lin"

    with Session(get_engine()) as session:
        admin_row = session.exec(select(User).where(User.username == "admin")).one()
        import_cases_to_requirement(
            session,
            req_id,
            [{"case_key": "TC-003", "title": "Agent 入库", "content_md": "正文", "priority": "P2"}],
            operator="admin",
            operator_user_id=int(admin_row.id),
            reason="agent:conv-1:msg-1",
        )
        session.commit()

        import_logs = session.exec(
            select(TestCaseOperationLog).where(TestCaseOperationLog.operation == "import")
        ).all()
        assert len(import_logs) == 1
        assert import_logs[0].operator == "admin" or import_logs[0].operator == "alice"

    cases = client.get("/api/cases", headers=_origin()).json()
    by_key = {row["case_key"]: row for row in cases}
    assert by_key["TC-001"]["created_by_username"] == "alice"
    assert by_key["TC-001"]["created_by_display_name"] == "Alice Lin"
    assert by_key["TC-002"]["created_by_username"] == "alice"
    assert by_key["TC-003"]["created_by_username"] == "admin"
    assert by_key["TC-003"]["created_by_display_name"] == "管理员"


def test_cases_default_newest_sort_and_multi_filters(tmp_app_data, monkeypatch):
    client = _auth_client(tmp_app_data, monkeypatch)
    project_id = _setup_admin(client)
    alice_id = _create_and_login_user(client, username="alice", display_name="Alice")

    # Re-login as admin so we can also inspect another project boundary.
    assert client.post("/api/auth/logout", headers=_origin()).status_code == 204
    assert client.post(
        "/api/auth/login",
        headers=_origin(),
        json={"username": "admin", "password": "CaseGen-Test1!"},
    ).status_code == 200
    other_project = client.post(
        "/api/projects",
        headers=_origin(),
        json={"name": "Other Space", "slug": "other-space"},
    ).json()

    base_time = datetime(2026, 11, 20, 10, 0, 0)
    with Session(get_engine()) as session:
        admin_row = session.exec(select(User).where(User.username == "admin")).one()
        req1 = Requirement(project_id=project_id, title="需求一", description="D1")
        req2 = Requirement(project_id=project_id, title="需求二", description="D2")
        req_other = Requirement(project_id=other_project["id"], title="他空间需求", description="D3")
        session.add_all([req1, req2, req_other])
        session.flush()

        c1 = TestCase(
            requirement_id=int(req1.id),
            case_key="TC-001",
            title="最早入库",
            content_md="body1",
            priority="P1",
            source_task_id=10,
            source_draft_id=1,
            created_by_user_id=alice_id,
            created_by_username="alice",
            created_at=base_time,
            updated_at=base_time + timedelta(days=3),
        )
        c2 = TestCase(
            requirement_id=int(req2.id),
            case_key="TC-002",
            title="中间入库",
            content_md="body2",
            priority="P0",
            source_task_id=None,
            created_by_user_id=int(admin_row.id),
            created_by_username="admin",
            created_at=base_time + timedelta(days=1),
            updated_at=base_time + timedelta(days=1),
        )
        c3 = TestCase(
            requirement_id=int(req1.id),
            case_key="TC-003",
            title="最新入库",
            content_md="body3",
            priority="P2",
            source_task_id=11,
            source_draft_id=2,
            created_by_user_id=alice_id,
            created_by_username="alice",
            created_at=base_time + timedelta(days=2),
            updated_at=base_time + timedelta(days=2),
        )
        c_other = TestCase(
            requirement_id=int(req_other.id),
            case_key="TC-999",
            title="他空间用例",
            content_md="other",
            created_by_username="bob",
            created_at=base_time + timedelta(days=5),
            updated_at=base_time + timedelta(days=5),
        )
        session.add_all([c1, c2, c3, c_other])
        session.commit()

    # Default sort is created_desc (newest created_at on top).
    default_list = client.get("/api/cases", headers=_origin()).json()
    assert [row["case_key"] for row in default_list] == ["TC-003", "TC-002", "TC-001"]

    # Explicit sort modes.
    assert [
        row["case_key"]
        for row in client.get("/api/cases", params={"sort": "created_asc"}, headers=_origin()).json()
    ] == ["TC-001", "TC-002", "TC-003"]
    assert [
        row["case_key"]
        for row in client.get("/api/cases", params={"sort": "updated_desc"}, headers=_origin()).json()
    ] == ["TC-001", "TC-003", "TC-002"]
    assert [
        row["case_key"]
        for row in client.get("/api/cases", params={"sort": "case_key_asc"}, headers=_origin()).json()
    ] == ["TC-001", "TC-003", "TC-002"]
    assert client.get("/api/cases", params={"sort": "bad_sort"}, headers=_origin()).status_code == 422

    # Filter by submitter (created_by).
    alice_cases = client.get(
        "/api/cases", params={"created_by": "ALICE"}, headers=_origin()
    ).json()
    assert [row["case_key"] for row in alice_cases] == ["TC-003", "TC-001"]

    # Filter by source_type.
    task_cases = client.get(
        "/api/cases", params={"source_type": "task"}, headers=_origin()
    ).json()
    assert [row["case_key"] for row in task_cases] == ["TC-003", "TC-001"]
    direct_cases = client.get(
        "/api/cases", params={"source_type": "direct"}, headers=_origin()
    ).json()
    assert [row["case_key"] for row in direct_cases] == ["TC-002"]
    assert client.get("/api/cases", params={"source_type": "other"}, headers=_origin()).status_code == 422

    # Filter by created_from / created_to (date-only and timezone-aware ISO).
    date_filtered = client.get(
        "/api/cases",
        params={"created_from": "2026-11-21", "created_to": "2026-11-21"},
        headers=_origin(),
    ).json()
    assert [row["case_key"] for row in date_filtered] == ["TC-002"]

    tz_filtered = client.get(
        "/api/cases",
        params={
            "created_from": "2026-11-22T00:00:00+08:00",
            "created_to": "2026-11-22T23:59:59+08:00",
        },
        headers=_origin(),
    ).json()
    assert [row["case_key"] for row in tz_filtered] == ["TC-003"]
    assert client.get("/api/cases", params={"created_from": "not-a-date"}, headers=_origin()).status_code == 422

    # Submitters list is scoped to current project.
    submitters = client.get("/api/cases/submitters", headers=_origin()).json()
    assert [item["username"] for item in submitters] == ["admin", "alice"]
