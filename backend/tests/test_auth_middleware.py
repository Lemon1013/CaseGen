"""Regression tests for the authentication middleware detached-User 500.

Under real uvicorn the middleware loads a SQLModel User in a short-lived
Session, then closes it.  get_user_for_token() may commit (it refreshes
last_seen_at), which expires every loaded attribute: a later
getattr(user, "is_active") on the detached object raises
DetachedInstanceError and turns an ordinary GET into a 500.  TestClient
does not naturally expire attributes, so these tests force that lifecycle
explicitly.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm.exc import DetachedInstanceError
from sqlmodel import Session, select

from app import config
from app.db import get_engine, init_db
from app.main import create_app
from app.models.entities import AuthSession, User
from app.services.auth import hash_password
from app.services.permissions import AuthIdentity, snapshot_user


def _origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


def _make_user(username: str, role: str = "user") -> int:
    with Session(get_engine()) as session:
        row = User(
            username=username,
            display_name=username.upper(),
            password_hash=hash_password("CaseGen-Test1!"),
            role=role,
            is_active=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return int(row.id)


def test_snapshot_survives_session_close_after_commit(tmp_app_data):
    """Direct reproduction: commit expires attributes; the ORM object breaks,
    the frozen snapshot does not."""

    init_db()  # create tables on this isolated database before seeding
    user_id = _make_user("bob")
    with Session(get_engine()) as session:
        row = session.get(User, user_id)
        identity = snapshot_user(row)  # must happen INSIDE the open session
        # Mirror get_user_for_token refreshing last_seen_at: the commit
        # expires all loaded instances by default (expire_on_commit=True).
        session.commit()
    with pytest.raises(DetachedInstanceError):
        _ = row.is_active
    # None of these may touch SQLAlchemy / the closed session.
    assert isinstance(identity, AuthIdentity)
    assert identity.id == user_id
    assert identity.username == "bob"
    assert identity.display_name == "BOB"
    assert identity.role == "user"
    assert identity.is_active is True
    # Frozen + slotted: no mutation, no lazy instrumentation.
    with pytest.raises(Exception):
        identity.role = "admin"  # type: ignore[misc]
    assert not hasattr(identity, "__dict__")


def test_middleware_snapshot_prevents_500(tmp_app_data, monkeypatch):
    """Force the real uvicorn lifecycle: the auth Session commits (expiring
    every attribute) before the middleware finishes.  Old code stored the
    detached ORM User and blew up in is_admin(); new code stores a snapshot."""

    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    import app.main as main_module
    from app.services import auth as auth_service

    original = auth_service.get_user_for_token

    def expiring_lookup(session, token):
        user = original(session, token)
        if user is not None:
            # Expire every loaded attribute exactly like a last_seen_at
            # refresh commit does in the live service.
            session.commit()
        return user

    # main.py imported the symbol directly, so patch the name it uses.
    monkeypatch.setattr(main_module, "get_user_for_token", expiring_lookup)

    admin = TestClient(create_app())
    assert admin.post(
        "/api/auth/setup", headers=_origin(),
            json={"username": "root", "display_name": "Root", "password": "CaseGen-Test1!"},
    ).status_code == 200
    with Session(get_engine()) as session:
        project_id = 1  # the default project seeded by migrations
    _make_user("member")
    member = TestClient(create_app())
    assert member.post(
        "/api/auth/login", headers=_origin(),
        json={"username": "member", "password": "CaseGen-Test1!"},
    ).status_code == 200

    # The exact failing request from nohup.out: an ordinary user hitting a
    # path with project_id, which passes through is_admin(request).
    resp = member.get("/api/models", params={"project_id": project_id}, headers=_origin())
    assert resp.status_code == 200, resp.text
    # A real project-scoped path exercises the visibility fallback too.
    tasks = member.get("/api/tasks", params={"project_id": project_id}, headers=_origin())
    assert tasks.status_code == 200, tasks.text
    # Ordinary user still forbidden on admin endpoints (no 500, no bypass).
    assert member.get("/api/users", headers=_origin()).status_code == 403
    # Administrator path remains healthy.
    assert admin.get("/api/users", headers=_origin()).status_code == 200
    assert admin.get("/api/models", params={"project_id": project_id}, headers=_origin()).status_code == 200


def test_session_and_me_still_use_live_session_user(tmp_app_data, monkeypatch):
    """/session and /me are in public_paths: the middleware does NOT inject
    a snapshot there, and they must keep working with their own live User."""

    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    client = TestClient(create_app())
    assert client.post(
        "/api/auth/setup", headers=_origin(),
            json={"username": "root", "password": "CaseGen-Test1!"},
    ).status_code == 200
    assert client.get("/api/auth/session").status_code == 200
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "root"
    assert me.json()["role"] == "admin"
