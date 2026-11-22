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
from app.models.entities import AgentConversation, AgentMessage, Project, User
from app.services.auth import hash_password


def _origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


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


def _client_for(username: str) -> TestClient:
    client = TestClient(create_app())
    assert client.post(
        "/api/auth/login",
        headers=_origin(),
        json={"username": username, "password": "CaseGen-Test1!"},
    ).status_code == 200
    return client


def _env(tmp_app_data, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    admin = TestClient(create_app())
    assert admin.post(
        "/api/auth/setup",
        headers=_origin(),
        json={"username": "root", "password": "CaseGen-Test1!"},
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

    # Write paths (message / cancel / delete / patch / attachment / save-cases) and SSE
    # all funnel through _get_conversation, so they reject foreign users AND admin.
    with Session(get_engine()) as session:
        b_msg = AgentMessage(
            conversation_id=b_conv["id"],
            role="assistant",
            content='```json\n[{"case_key": "TC-901", "title": "私密用例", "steps": [{"action": "a", "expected": "b"}]}]\n```',
        )
        session.add(b_msg)
        session.commit()
        session.refresh(b_msg)
        b_msg_id = str(b_msg.id)

    for caller in (alice, admin):
        assert caller.patch(
            f"/api/agent/conversations/{b_conv['id']}?project_id={pid}",
            headers=_origin(),
            json={"title": "Hijacked"},
        ).status_code == 404
        assert caller.post(
            f"/api/agent/conversations/{b_conv['id']}/attachments?project_id={pid}",
            headers=_origin(),
            files={"file": ("note.md", b"secret", "text/markdown")},
        ).status_code == 404
        assert caller.post(
            f"/api/agent/conversations/{b_conv['id']}/save-cases?project_id={pid}",
            headers=_origin(),
            json={
                "message_id": b_msg_id,
                "target_mode": "new_requirement",
                "requirement_title": "尝试窃取",
            },
        ).status_code == 404
        assert caller.post(
            f"/api/agent/conversations/{b_conv['id']}/messages?project_id={pid}",
            headers=_origin(),
            json={"content": "hi", "idempotency_key": f"k-{id(caller)}"},
        ).status_code == 404
        assert caller.post(
            f"/api/agent/conversations/{b_conv['id']}/cancel?project_id={pid}",
            headers=_origin(),
        ).status_code == 404
        assert caller.delete(
            f"/api/agent/conversations/{b_conv['id']}?project_id={pid}", headers=_origin()
        ).status_code == 404
        assert caller.get(f"/api/agent/conversations/{b_conv['id']}/stream?project_id={pid}").status_code == 404

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


def test_hidden_project_and_unauthenticated_fail_closed(tmp_app_data, monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.api import agent_chat

    admin, _pid = _env(tmp_app_data, monkeypatch)
    _make_user("alice")
    alice = _client_for("alice")

    # Hidden project (is_public=False) rejects non-admin access with 404 on /api/agent/*.
    hidden = admin.post(
        "/api/projects",
        headers=_origin(),
        json={"name": "Secret Proj", "slug": "secret-proj"},
    )
    assert hidden.status_code in (200, 201), hidden.text
    hidden_pid = hidden.json()["id"]
    upd = admin.put(
        f"/api/projects/{hidden_pid}?project_id={hidden_pid}",
        headers=_origin(),
        json={"is_public": False},
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["is_public"] is False
    hidden_conv = _create(admin, hidden_pid, "Admin hidden proj conv")
    assert alice.get(f"/api/agent/conversations?project_id={hidden_pid}").status_code == 404
    assert alice.get(
        f"/api/agent/conversations/{hidden_conv['id']}?project_id={hidden_pid}"
    ).status_code == 404

    # _owner_id must fail closed with 401 when AUTH_ENABLED=True and request.state.user is absent.
    scope = {"type": "http", "method": "GET", "path": "/api/agent/conversations", "headers": []}
    req = Request(scope)
    try:
        agent_chat._owner_id(req)
        raise AssertionError("Expected HTTPException(401) when AUTH_ENABLED=True and user is missing")
    except HTTPException as exc:
        assert exc.status_code == 401
