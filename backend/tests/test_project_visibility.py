"""Project visibility (projects.is_public) contract.

Ordinary users only see projects an administrator marked as public, and the
middleware makes hidden projects unreachable on project DATA paths such as
/api/tasks and /api/cases - but must NOT block /api/projects (the escape
hatch for switching away) or /api/prompts (globally visible and editable).
Administrators are unrestricted.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import get_engine
from app.main import create_app
from app.models.entities import Project, User
from app.services.auth import hash_password


def _origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


def _env(monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    return TestClient(create_app())


def _setup_admin(client: TestClient) -> None:
    response = client.post(
        "/api/auth/setup",
        headers=_origin(),
        json={"username": "admin", "display_name": "Admin", "password": "CaseGen-Test1!"},
    )
    assert response.status_code == 200, response.text
    with Session(get_engine()) as session:
        project = session.exec(select(Project)).first()
        client.params = {"project_id": int(project.id)}


def _make_user(username: str, role: str = "user") -> int:
    with Session(get_engine()) as session:
        row = User(
            username=username,
            display_name=username,
            password_hash=hash_password("CaseGen-Test1!"),
            role=role,
            is_active=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return int(row.id)


def _login(username: str) -> TestClient:
    client = TestClient(create_app())
    assert client.post(
        "/api/auth/login",
        headers=_origin(),
        json={"username": username, "password": "CaseGen-Test1!"},
    ).status_code == 200
    return client


def _set_public(admin: TestClient, project_id: int, value: bool) -> None:
    response = admin.put(
        f"/api/projects/{project_id}",
        headers=_origin(),
        params={"project_id": project_id},
        json={"is_public": value},
    )
    assert response.status_code == 200, response.text
    assert response.json()["is_public"] is value


def test_list_hides_non_public_projects_from_ordinary_users(tmp_app_data, monkeypatch):
    admin = _env(monkeypatch)
    _setup_admin(admin)
    hidden = admin.post("/api/projects", headers=_origin(), json={"name": "Hidden", "slug": "hidden"}).json()
    visible = admin.post("/api/projects", headers=_origin(), json={"name": "Visible", "slug": "visible"}).json()
    assert hidden["is_public"] is True
    assert visible["is_public"] is True

    _make_user("member")
    member = _login("member")
    listed = member.get("/api/projects", headers=_origin())
    assert listed.status_code == 200
    assert "Hidden" in {item["name"] for item in listed.json()}

    # Flip one project off; the change takes effect immediately.
    _set_public(admin, hidden["id"], False)
    names_after = {item["name"] for item in member.get("/api/projects", headers=_origin()).json()}
    assert "Hidden" not in names_after
    assert "Visible" in names_after

    # Administrator still sees every project, including single detail.
    admin_names = {item["name"] for item in admin.get("/api/projects", headers=_origin()).json()}
    assert {"Hidden", "Visible"}.issubset(admin_names)
    assert admin.get(
        f"/api/projects/{hidden['id']}",
        params={"project_id": hidden["id"]},
        headers=_origin(),
    ).status_code == 200

    _set_public(admin, hidden["id"], True)
    assert "Hidden" in {item["name"] for item in member.get("/api/projects", headers=_origin()).json()}


def test_hidden_project_is_404_on_every_scoped_path(tmp_app_data, monkeypatch):
    admin = _env(monkeypatch)
    _setup_admin(admin)
    hidden = admin.post("/api/projects", headers=_origin(), json={"name": "Secret", "slug": "secret"}).json()
    with Session(get_engine()) as session:
        default_id = int(session.exec(select(Project)).first().id)
    _set_public(admin, hidden["id"], False)

    _make_user("member")
    member = _login("member")
    params = {"project_id": hidden["id"]}
    assert member.get("/api/tasks", headers=_origin(), params=params).status_code == 404
    assert member.get("/api/tasks", headers=_origin(), params=params).json() == {"detail": "Resource not found in project"}
    assert member.get("/api/cases", headers=_origin(), params=params).status_code == 404
    assert member.get(f"/api/projects/{hidden['id']}", headers=_origin(), params=params).status_code == 404
    assert member.get("/api/tasks", headers=_origin(), params={"project_id": default_id}).status_code == 200
    assert admin.get("/api/tasks", headers=_origin(), params=params).status_code == 200


def test_ordinary_user_cannot_change_visibility(tmp_app_data, monkeypatch):
    admin = _env(monkeypatch)
    _setup_admin(admin)
    with Session(get_engine()) as session:
        default_id = int(session.exec(select(Project)).first().id)
    _make_user("member")
    member = _login("member")
    assert member.put(
        f"/api/projects/{default_id}",
        params={"project_id": default_id},
        headers=_origin(),
        json={"is_public": False},
    ).status_code == 403


def test_hidden_current_project_does_not_lock_user_out(tmp_app_data, monkeypatch):
    """D-1: client.ts injects project_id into every /api request.

    When the cached/current project becomes hidden, the user must still be
    able to list projects (to switch away), use prompts, and read global
    config.  Only project DATA endpoints may return 404.
    """
    admin = _env(monkeypatch)
    _setup_admin(admin)
    hidden = admin.post("/api/projects", headers=_origin(), json={"name": "SoonHidden", "slug": "soon-hidden"}).json()
    visible = admin.post("/api/projects", headers=_origin(), json={"name": "StillVisible", "slug": "still-visible"}).json()
    _set_public(admin, hidden["id"], False)

    _make_user("member")
    member = _login("member")
    # Simulate the browser: every request carries the now-hidden project id.
    member.params = {"project_id": hidden["id"]}

    listed = member.get("/api/projects", headers=_origin())
    assert listed.status_code == 200, listed.text
    names = {item["name"] for item in listed.json()}
    assert "StillVisible" in names
    assert "SoonHidden" not in names

    # Prompts are global: visible and editable for everyone, never gated.
    assert member.get("/api/prompts", headers=_origin()).status_code == 200
    # Other global/config planes are reachable too (read side).
    assert member.get("/api/models", headers=_origin()).status_code == 200
    assert member.get("/api/auth/bootstrap", headers=_origin()).status_code == 200

    # Project DATA on the hidden id is still a 404.
    assert member.get("/api/tasks", headers=_origin()).status_code == 404
    assert member.get("/api/cases", headers=_origin()).status_code == 404
    # Switching to a visible project restores normal access.
    assert member.get("/api/tasks", headers=_origin(), params={"project_id": visible["id"]}).status_code == 200

