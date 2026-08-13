from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import get_engine
from app.main import create_app
from app.models.entities import AuthSession, User


def _auth_client(tmp_app_data, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    return TestClient(create_app())


def _origin():
    return {"Origin": "http://testserver"}


def _setup(client: TestClient) -> None:
    response = client.post(
        "/api/auth/setup",
        headers=_origin(),
        json={"username": "admin", "display_name": "Admin", "password": "password1234"},
    )
    assert response.status_code == 200


def test_setup_login_logout_and_origin_guard(tmp_app_data, monkeypatch):
    client = _auth_client(tmp_app_data, monkeypatch)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/tasks").status_code == 401
    assert client.post("/api/auth/setup", json={"username": "admin", "password": "password1234"}).status_code == 403

    _setup(client)
    assert client.post(
        "/api/auth/login",
        headers=_origin(),
        json={"username": "unknown", "password": "password1234"},
    ).status_code == 401
    assert client.post("/api/auth/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/auth/logout", headers=_origin()).status_code == 204
    assert client.get("/api/tasks").status_code == 401

    with Session(get_engine()) as session:
        user = session.exec(select(User)).one()
        assert user.password_hash.startswith("scrypt$")
        assert "password1234" not in user.password_hash
        assert session.exec(select(AuthSession)).all() == []


def test_cases_edit_conflict_logs_archive_and_export(tmp_app_data, monkeypatch):
    client = _auth_client(tmp_app_data, monkeypatch)
    _setup(client)
    headers = _origin()
    requirement = client.post(
        "/api/requirements", headers=headers, json={"title": "R", "description": "D"}
    ).json()
    created = client.post(
        "/api/cases",
        headers=headers,
        json={
            "requirement_id": requirement["id"],
            "case_key": "TC-001",
            "title": "登录",
            "content_md": "步骤",
        },
    )
    assert created.status_code == 201
    case = created.json()
    changed = client.patch(
        f"/api/cases/{case['id']}",
        headers=headers,
        json={"content_md": "新步骤", "expected_revision": 1},
    )
    assert changed.status_code == 200
    assert client.patch(
        f"/api/cases/{case['id']}",
        headers=headers,
        json={"content_md": "过期写入", "expected_revision": 1},
    ).status_code == 409
    assert client.post(
        f"/api/cases/{case['id']}/archive?expected_revision=2", headers=headers
    ).json()["status"] == "archived"
    assert client.get("/api/cases", headers=headers).json() == []
    logs = client.get(f"/api/cases/{case['id']}/logs", headers=headers).json()
    assert [row["operation"] for row in logs] == ["create", "edit", "archive"]
    assert all("新步骤" not in row for row in logs)
    assert all(
        field not in logs[0]
        for field in ("before_content_md", "after_content_md", "diff_text", "diff_json")
    )
    exported = client.get(
        f"/api/cases/{case['id']}/export", headers=headers
    )
    assert exported.status_code == 409
    assert client.post(
        f"/api/cases/{case['id']}/restore?expected_revision=3", headers=headers
    ).status_code == 200
    exported = client.get(f"/api/cases/{case['id']}/export", headers=headers)
    assert exported.status_code == 200
    assert "attachment" in exported.headers["content-disposition"]
    assert "新步骤" in exported.content.decode("utf-8")
    assert client.get(f"/api/cases/{case['id']}/logs", headers=headers).json()[-1]["operation"] == "export"
