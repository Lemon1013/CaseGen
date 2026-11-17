"""Administrator-only user management API contract.

Covers the full lifecycle agreed in the plan: setup -> admin creates a normal
user -> that user logs in -> privilege escalation is rejected -> disabling the
account kills its live session -> list responses never leak the hash.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import get_engine
from app.main import create_app
from app.models.entities import AuthSession, User


def _origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


def _auth_client(tmp_app_data, monkeypatch) -> TestClient:
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    return TestClient(create_app())


def _setup_admin(client: TestClient) -> dict:
    response = client.post(
        "/api/auth/setup",
        headers=_origin(),
        json={"username": "rootadmin", "display_name": "Root", "password": "password1234"},
    )
    assert response.status_code == 200, response.text
    with Session(get_engine()) as session:
        from app.models.entities import Project

        project = session.exec(select(Project)).first()
        client.params = {"project_id": int(project.id)}
    return response.json()["user"]


def test_user_lifecycle_list_create_login_escalation_disable(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    admin = _setup_admin(admin_client)

    # The list starts with exactly the setup admin and never exposes the hash.
    listed = admin_client.get("/api/users", headers=_origin())
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["username"] == "rootadmin"
    assert rows[0]["role"] == "admin"
    assert "password_hash" not in rows[0]
    assert "password1234" not in str(rows)

    # Admin creates a normal user.
    created = admin_client.post(
        "/api/users",
        headers=_origin(),
        json={
            "username": "alice",
            "display_name": "Alice",
            "password": "password1234",
            "role": "user",
        },
    )
    assert created.status_code == 201, created.text
    alice = created.json()
    assert alice["username"] == "alice"
    assert alice["role"] == "user"
    assert alice["is_active"] is True
    assert "password_hash" not in alice
    alice_id = alice["id"]

    # Duplicate usernames are rejected with 409.
    duplicate = admin_client.post(
        "/api/users",
        headers=_origin(),
        json={"username": "alice", "password": "password1234"},
    )
    assert duplicate.status_code == 409

    # The new user can log in in a separate client.
    alice_client = TestClient(create_app())
    login = alice_client.post(
        "/api/auth/login",
        headers=_origin(),
        json={"username": "alice", "password": "password1234"},
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["role"] == "user"

    # A normal user cannot use any admin-only endpoint.
    assert alice_client.get("/api/users", headers=_origin()).status_code == 403
    assert alice_client.post(
        "/api/users", headers=_origin(),
        json={"username": "bob", "password": "password1234"},
    ).status_code == 403
    assert alice_client.post(
        "/api/models", headers=_origin(),
        json={"name": "m", "base_url": "http://x", "api_key": "k", "model_name": "m"},
    ).status_code == 403
    assert alice_client.patch(
        f"/api/users/{alice_id}", headers=_origin(), json={"role": "admin"}
    ).status_code == 403
    # Query endpoints remain available to every authenticated user.
    assert alice_client.get("/api/models", headers=_origin()).status_code == 200
    assert alice_client.get("/api/prompts", headers=_origin()).status_code == 200

    # Admin edits the user: role promotion is reflected in the list projection.
    promoted = admin_client.patch(
        f"/api/users/{alice_id}", headers=_origin(), json={"role": "admin"}
    )
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "admin"

    # Reset credentials invalidates existing sessions.
    reset = admin_client.post(
        f"/api/users/{alice_id}/reset-password",
        headers=_origin(),
        json={"password": "passwordnew"},
    )
    assert reset.status_code == 200, reset.text
    assert "password_hash" not in reset.json()

    # Disabling the account must immediately invalidate its old session.
    with Session(get_engine()) as session:
        session.add(
            User(
                username="charlie",
                display_name="Charlie",
                password_hash="scrypt$invalid",
                role="user",
                is_active=True,
            )
        )
        session.commit()
    charlie_client = TestClient(create_app())
    charlie_id = admin_client.get("/api/users", headers=_origin()).json()
    charlie_id = next(row["id"] for row in charlie_id if row["username"] == "charlie")

    disabled = admin_client.patch(
        f"/api/users/{charlie_id}", headers=_origin(), json={"is_active": False}
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False
    # The disable path revokes sessions; a fresh login for an inactive account
    # is refused by the authentication service as well.
    assert charlie_client.post(
        "/api/auth/login", headers=_origin(),
        json={"username": "charlie", "password": "password1234"},
    ).status_code == 401


def test_stale_session_after_disable_returns_401(tmp_app_data, monkeypatch):
    """A user disabled after login loses access on the very next request."""

    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)
    created = admin_client.post(
        "/api/users", headers=_origin(),
        json={"username": "dave", "password": "password1234", "role": "user"},
    )
    assert created.status_code == 201
    dave_id = created.json()["id"]

    dave_client = TestClient(create_app())
    assert dave_client.post(
        "/api/auth/login", headers=_origin(),
        json={"username": "dave", "password": "password1234"},
    ).status_code == 200
    assert dave_client.get("/api/prompts", headers=_origin()).status_code == 200

    assert admin_client.patch(
        f"/api/users/{dave_id}", headers=_origin(), json={"is_active": False}
    ).status_code == 200
    # Both the revoked cookie and the inactive flag keep the account out.
    assert dave_client.get("/api/prompts", headers=_origin()).status_code == 401
    assert dave_client.post(
        "/api/auth/login", headers=_origin(),
        json={"username": "dave", "password": "password1234"},
    ).status_code == 401


def test_users_router_requires_authentication(tmp_app_data, monkeypatch):
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    # No /api/users entry in public_paths: anonymous callers get 401.
    assert admin_client.get("/api/users").status_code == 401
    _setup_admin(admin_client)
    assert admin_client.get("/api/users", headers=_origin()).status_code == 200


def test_last_active_admin_cannot_be_disabled_or_demoted(tmp_app_data, monkeypatch):
    """A-1: keep at least one usable administrator at all times."""
    admin_client = _auth_client(tmp_app_data, monkeypatch)
    admin = _setup_admin(admin_client)
    admin_id = admin["id"]
    origin = _origin()

    # The only admin cannot disable or demote themselves.
    assert admin_client.patch(
        f"/api/users/{admin_id}", headers=origin, json={"is_active": False}
    ).status_code == 409
    assert admin_client.patch(
        f"/api/users/{admin_id}", headers=origin, json={"role": "user"}
    ).status_code == 409
    # Unchanged and still usable.
    assert admin_client.get("/api/users", headers=origin).status_code == 200

    # A second admin makes it safe to disable/demote one of them.
    second = admin_client.post(
        "/api/users", headers=origin,
        json={"username": "secondadmin", "password": "password1234", "role": "admin"},
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]
    # Disabling the *other* admin is allowed: the logged-in one remains.
    assert admin_client.patch(
        f"/api/users/{second_id}", headers=origin, json={"is_active": False}
    ).status_code == 200
    # The remaining admin still has full access.
    assert admin_client.get("/api/users", headers=origin).status_code == 200
    # Demoting the other admin is likewise allowed with a spare admin present.
    assert admin_client.patch(
        f"/api/users/{second_id}", headers=origin, json={"role": "user", "is_active": True}
    ).status_code == 200

    # Ordinary users are unaffected by the guard.
    created = admin_client.post(
        "/api/users", headers=origin,
        json={"username": "plain", "password": "password1234", "role": "user"},
    )
    assert created.status_code == 201
    assert admin_client.patch(
        f"/api/users/{created.json()['id']}", headers=origin, json={"is_active": False}
    ).status_code == 200

    # Resetting the admin password is not blocked by the guard.
    reset = admin_client.post(
        f"/api/users/{second_id}/reset-password", headers=origin,
        json={"password": "passwordnew"},
    )
    assert reset.status_code == 200, reset.text



def test_model_ping_requires_admin(tmp_app_data, monkeypatch):
    """B1: ping is an unsafe config operation using the plaintext api_key."""
    from app.models.entities import ModelConfig

    admin_client = _auth_client(tmp_app_data, monkeypatch)
    _setup_admin(admin_client)
    origin = _origin()
    with Session(get_engine()) as session:
        row = ModelConfig(
            name="m", base_url="http://127.0.0.1:1", api_key="secret",
            model_name="m", is_default=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        model_id = int(row.id)

    from app.services.auth import hash_password
    with Session(get_engine()) as session:
        session.add(User(
            username="member",
            display_name="Member",
            password_hash=hash_password("password1234"),
            role="user",
            is_active=True,
        ))
        session.commit()
    member = TestClient(create_app())
    assert member.post(
        "/api/auth/login", headers=origin,
        json={"username": "member", "password": "password1234"},
    ).status_code == 200
    assert member.post(f"/api/models/{model_id}/ping", headers=origin).status_code == 403
    # Read side stays open for every signed-in user.
    assert member.get("/api/models", headers=origin).status_code == 200

