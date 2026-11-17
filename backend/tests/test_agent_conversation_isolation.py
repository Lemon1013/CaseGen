"""Agent conversations are private to the signed-in user.

Covers the full chain: create writes user_id, list filters by user_id, and
detail / message / cancel / SSE / delete all go through _get_conversation,
which rejects a foreign owner with the same 404 as a foreign project.
Administrators get no exception.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import get_engine
from app.main import create_app
from app.models.entities import AgentConversation, Project, User
from app.services.auth import hash_password


def _origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


def _make_user(username: str, role: str = "user") -> int:
    with Session(get_engine()) as session:
        row = User(
            username=username,
            display_name=username,
            password_hash=hash_password("password1234"),
            role=role,
            is_active=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return int(row.id)


def _client_for(username: str) -> TestClient:
    client = TestClient(create_app())
    assert client.post(
        "/api/auth/login",
        headers=_origin(),
        json={"username": username, "password": "password1234"},
    ).status_code == 200
    return client


def _env(tmp_app_data, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    admin = TestClient(create_app())
    assert admin.post(
        "/api/auth/setup",
        headers=_origin(),
        json={"username": "root", "password": "password1234"},
    ).status_code == 200
    with Session(get_engine()) as session:
        project_id = int(session.exec(select(Project)).first().id)
    return admin, project_id


def _create(client: TestClient, project_id: int, title: str) -> dict:
    resp = client.post(
        f"/api/agent/conversations?project_id={project_id}",
        headers=_origin(),
        json={"title": title},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_conversations_are_private_between_users_and_from_admin(tmp_app_data, monkeypatch):
    admin, pid = _env(tmp_app_data, monkeypatch)
    _make_user("alice")
    _make_user("bob")
    alice = _client_for("alice")
    bob = _client_for("bob")

    a_conv = _create(alice, pid, "Alice private")
    b_conv = _create(bob, pid, "Bob private")
    assert a_conv["id"] != b_conv["id"]

    # The owner column is actually persisted.
    with Session(get_engine()) as session:
        users = {row.username: int(row.id) for row in session.exec(select(User)).all()}
        stored = {row.id: row.user_id for row in session.exec(select(AgentConversation)).all()}
        assert stored[a_conv["id"]] == users["alice"]
        assert stored[b_conv["id"]] == users["bob"]

    # Each user only lists their own conversation.
    assert {item["id"] for item in alice.get(f"/api/agent/conversations?project_id={pid}").json()} == {a_conv["id"]}
    assert {item["id"] for item in bob.get(f"/api/agent/conversations?project_id={pid}").json()} == {b_conv["id"]}

    # A6-2: a foreign conversation and a non-existent one must be
    # indistinguishable (same status AND same neutral detail), otherwise a
    # caller can enumerate whether another user's chat exists.
    foreign = alice.get(f"/api/agent/conversations/{b_conv['id']}?project_id={pid}")
    missing = alice.get(f"/api/agent/conversations/does-not-exist?project_id={pid}")
    assert foreign.status_code == 404
    assert missing.status_code == foreign.status_code
    assert missing.json() == foreign.json()
    assert foreign.json()["detail"] == "Resource not found in project"
    assert bob.get(f"/api/agent/conversations/{a_conv['id']}?project_id={pid}").status_code == 404

    # Administrators are not exempt: chat stays private.
    assert admin.get(f"/api/agent/conversations/{a_conv['id']}?project_id={pid}").status_code == 404
    assert admin.get(f"/api/agent/conversations/{b_conv['id']}?project_id={pid}").status_code == 404
    assert admin.get(f"/api/agent/conversations?project_id={pid}").json() == []

    # Write paths (message / cancel / delete) and SSE all funnel through
    # _get_conversation, so they reject a foreign conversation too.
    assert alice.post(
        f"/api/agent/conversations/{b_conv['id']}/messages?project_id={pid}",
        headers=_origin(),
        json={"content": "hi", "idempotency_key": "k1"},
    ).status_code == 404
    assert alice.post(
        f"/api/agent/conversations/{b_conv['id']}/cancel?project_id={pid}",
        headers=_origin(),
    ).status_code == 404
    assert alice.delete(
        f"/api/agent/conversations/{b_conv['id']}?project_id={pid}", headers=_origin()
    ).status_code == 404
    assert alice.get(f"/api/agent/conversations/{b_conv['id']}/stream?project_id={pid}").status_code == 404

    # Owners keep full access to their own conversation.
    assert alice.get(f"/api/agent/conversations/{a_conv['id']}?project_id={pid}").status_code == 200
    assert bob.get(f"/api/agent/conversations/{b_conv['id']}?project_id={pid}").status_code == 200


def test_sse_stream_does_not_leak_foreign_snapshot(tmp_app_data, monkeypatch):
    from app.services.agent_stream import AgentStreamBroker
    from app.api import agent_chat

    _admin, pid = _env(tmp_app_data, monkeypatch)
    _make_user("alice")
    _make_user("bob")
    alice = _client_for("alice")
    bob = _client_for("bob")
    monkeypatch.setattr(agent_chat, "agent_stream", AgentStreamBroker())
    conv = _create(alice, pid, "Alice stream")
    with Session(get_engine()) as session:
        row = session.get(AgentConversation, conv["id"])
        row.status = "busy"
        session.add(row)
        session.commit()
    agent_chat.agent_stream.start(conv["id"], status="busy")
    agent_chat.agent_stream.complete(conv["id"], text="private answer", status="active")

    # Bob must not receive Alice's terminal snapshot.
    assert bob.get(f"/api/agent/conversations/{conv['id']}/stream?project_id={pid}").status_code == 404
    assert alice.get(f"/api/agent/conversations/{conv['id']}/stream?project_id={pid}").status_code == 200
