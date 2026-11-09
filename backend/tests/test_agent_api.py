"""Tests for the /api/agent REST + SSE plane (P1 DSH agent integration).

Every test isolates SQLite/Wiki/upload paths via tmp_app_data and swaps the
in-memory broker plus the worker-thread starter with fakes, so no test touches a
real DSH instance or the network.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import config
from app.db import get_engine
from app.main import create_app
from app.models.entities import AgentConversation, AgentMessage
from app.services.agent_stream import AgentStreamBroker
from app.services.task_stream import TaskStreamEvent, TaskStreamPoll


# --------------------------------------------------------------------------
# fixtures & helpers
# --------------------------------------------------------------------------


@pytest.fixture
def agent_env(tmp_app_data, monkeypatch):
    """Isolated app + fake broker + fake turn starter."""

    from app.api import agent_chat
    from app.services import agent_turn

    broker = AgentStreamBroker()
    monkeypatch.setattr(agent_chat, "agent_stream", broker)

    start_calls = {"count": 0}

    def _fake_start_turn(conversation_id, *, stream_id, transport=None):
        start_calls["count"] += 1
        return None

    monkeypatch.setattr(agent_turn, "start_turn", _fake_start_turn)

    client = TestClient(create_app())
    return SimpleNamespace(
        client=client,
        broker=broker,
        start_calls=start_calls,
        monkeypatch=monkeypatch,
    )


def _make_project(client, slug="owner"):
    resp = client.post("/api/projects", json={"name": slug, "slug": slug})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _make_conversation(client, project_id, **body):
    resp = client.post(f"/api/agent/conversations?project_id={project_id}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _set_status(conversation_id, status):
    with Session(get_engine()) as session:
        row = session.get(AgentConversation, str(conversation_id))
        assert row is not None
        row.status = status
        session.add(row)
        session.commit()


def _add_assistant_message(conversation_id, content):
    with Session(get_engine()) as session:
        session.add(
            AgentMessage(
                conversation_id=str(conversation_id),
                role="assistant",
                content=content,
            )
        )
        session.commit()


def _parse_sse(text):
    """Split an SSE body into framed events (drop keep-alive comments)."""

    events = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block or block.startswith(":"):
            continue
        fields = {}
        data = None
        for line in block.split("\n"):
            if line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            if key == "id":
                fields["id"] = int(value)
            elif key == "data":
                data = json.loads(value)
            else:
                fields[key] = value
        fields["data"] = data if data is not None else {}
        if "event" in fields:
            events.append(fields)
    return events


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


def test_create_and_list_conversations(agent_env):
    client = agent_env.client
    pid = _make_project(client)

    created = _make_conversation(client, pid, title="我的会话")
    assert created["title"] == "我的会话"
    assert created["status"] == "active"
    assert created["project_id"] == str(pid)
    assert created["message_count"] == 0

    listed = client.get(f"/api/agent/conversations?project_id={pid}")
    assert listed.status_code == 200
    ids = [item["id"] for item in listed.json()]
    assert created["id"] in ids


def test_create_conversation_defaults_title(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    created = _make_conversation(client, pid)
    assert created["title"] == "未命名对话"


def test_get_conversation_detail(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    created = _make_conversation(client, pid)

    detail = client.get(f"/api/agent/conversations/{created['id']}?project_id={pid}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == created["id"]
    assert body["messages"] == []
    assert body["message_count"] == 0


def test_patch_conversation_enforces_version(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    created = _make_conversation(client, pid)

    ok = client.patch(
        f"/api/agent/conversations/{created['id']}?project_id={pid}",
        json={"title": "改名了", "expected_version": created["version"]},
    )
    assert ok.status_code == 200
    updated = ok.json()
    assert updated["title"] == "改名了"
    assert updated["version"] == created["version"] + 1

    stale = client.patch(
        f"/api/agent/conversations/{created['id']}?project_id={pid}",
        json={"title": "又改", "expected_version": created["version"]},
    )
    assert stale.status_code == 409


def test_delete_conversation(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    created = _make_conversation(client, pid)

    deleted = client.delete(f"/api/agent/conversations/{created['id']}?project_id={pid}")
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"

    gone = client.get(f"/api/agent/conversations/{created['id']}?project_id={pid}")
    assert gone.status_code == 404


def test_project_id_required(agent_env):
    client = agent_env.client
    resp = client.get("/api/agent/conversations")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "project_id is required"


def test_foreign_project_conversation_is_404(agent_env):
    client = agent_env.client
    owner = _make_project(client, "owner")
    outsider = _make_project(client, "outsider")
    conv = _make_conversation(client, owner)

    resp = client.get(f"/api/agent/conversations/{conv['id']}?project_id={outsider}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Resource not found in project"


# --------------------------------------------------------------------------
# post message / idempotency / lifecycle
# --------------------------------------------------------------------------


def test_post_message_starts_turn_and_titles(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    conv = _make_conversation(client, pid)
    content = "请帮我生成登录用例"

    resp = client.post(
        f"/api/agent/conversations/{conv['id']}/messages?project_id={pid}",
        json={"content": content, "idempotency_key": "k1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["duplicated"] is False
    assert body["message"]["role"] == "user"
    assert body["message"]["content"] == content
    assert body["conversation"]["status"] == "busy"
    # Default title replaced by the first message.
    assert body["conversation"]["title"] == content
    assert agent_env.start_calls["count"] == 1


def test_post_message_busy_conflict(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    conv = _make_conversation(client, pid)
    _set_status(conv["id"], "busy")

    resp = client.post(
        f"/api/agent/conversations/{conv['id']}/messages?project_id={pid}",
        json={"content": "hi", "idempotency_key": "k1"},
    )
    assert resp.status_code == 409


def test_post_message_closed_conflict(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    conv = _make_conversation(client, pid)
    _set_status(conv["id"], "closed")

    resp = client.post(
        f"/api/agent/conversations/{conv['id']}/messages?project_id={pid}",
        json={"content": "hi", "idempotency_key": "k1"},
    )
    assert resp.status_code == 409


def test_post_message_idempotent_duplicate(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    conv = _make_conversation(client, pid)

    first = client.post(
        f"/api/agent/conversations/{conv['id']}/messages?project_id={pid}",
        json={"content": "生成用例", "idempotency_key": "same-key"},
    )
    assert first.status_code == 200
    assert first.json()["duplicated"] is False
    assert agent_env.start_calls["count"] == 1

    # Simulate the turn finishing so the conversation is idle again; a retried
    # POST with the same key must return the stored message and start no turn.
    _set_status(conv["id"], "active")

    second = client.post(
        f"/api/agent/conversations/{conv['id']}/messages?project_id={pid}",
        json={"content": "生成用例", "idempotency_key": "same-key"},
    )
    assert second.status_code == 200
    assert second.json()["duplicated"] is True
    assert second.json()["message"]["id"] == first.json()["message"]["id"]
    assert agent_env.start_calls["count"] == 1


def test_delete_busy_conversation_conflict(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    conv = _make_conversation(client, pid)
    _set_status(conv["id"], "busy")

    resp = client.delete(f"/api/agent/conversations/{conv['id']}?project_id={pid}")
    assert resp.status_code == 409


def test_cancel_idle_is_soft_noop(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    conv = _make_conversation(client, pid)

    resp = client.post(f"/api/agent/conversations/{conv['id']}/cancel?project_id={pid}")
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is False


def test_cancel_busy_requests_stop(agent_env):
    from app.services import agent_turn

    client = agent_env.client
    pid = _make_project(client)
    conv = _make_conversation(client, pid)
    agent_env.monkeypatch.setattr(agent_turn, "request_cancel", lambda cid: True)

    resp = client.post(f"/api/agent/conversations/{conv['id']}/cancel?project_id={pid}")
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is True


def test_agent_disabled_returns_410(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    agent_env.monkeypatch.setattr(config, "AGENT_ENABLED", False)

    assert client.get(f"/api/agent/conversations?project_id={pid}").status_code == 410
    assert client.get(f"/api/agent/skills?project_id={pid}").status_code == 410
    assert (
        client.post(f"/api/agent/conversations?project_id={pid}", json={}).status_code
        == 410
    )


def test_skills_lists_array(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    resp = client.get(f"/api/agent/skills?project_id={pid}")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    for item in resp.json():
        assert "name" in item


# --------------------------------------------------------------------------
# save-as-draft
# --------------------------------------------------------------------------


def test_save_as_draft_success(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    conv = _make_conversation(client, pid)
    assistant_answer = """
好的，用例：
```json
[{"case_key": "TC-001", "title": "登录成功", "steps": [{"action": "输入正确账号密码", "expected": "进入首页"}]}]
```
"""
    _add_assistant_message(conv["id"], assistant_answer)

    resp = client.post(
        f"/api/agent/conversations/{conv['id']}/save-as-draft?project_id={pid}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["imported_count"] >= 1
    assert body["task_id"] > 0
    assert body["draft_id"] > 0
    assert isinstance(body["case_keys"], list)


def test_save_as_draft_without_assistant_is_422(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    conv = _make_conversation(client, pid)

    resp = client.post(
        f"/api/agent/conversations/{conv['id']}/save-as-draft?project_id={pid}"
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# SSE stream
# --------------------------------------------------------------------------


def test_stream_idle_conversation_409(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    conv = _make_conversation(client, pid)

    resp = client.get(f"/api/agent/conversations/{conv['id']}/stream?project_id={pid}")
    assert resp.status_code == 409


def test_stream_busy_without_snapshot_409(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    conv = _make_conversation(client, pid)
    _set_status(conv["id"], "busy")

    resp = client.get(f"/api/agent/conversations/{conv['id']}/stream?project_id={pid}")
    assert resp.status_code == 409


def test_stream_serves_terminal_completed_snapshot(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    conv = _make_conversation(client, pid)
    _set_status(conv["id"], "busy")
    agent_env.broker.start(conv["id"], status="busy")
    agent_env.broker.complete(conv["id"], text="答案", status="active")

    resp = client.get(f"/api/agent/conversations/{conv['id']}/stream?project_id={pid}")
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert events[0]["event"] == "snapshot"
    assert events[0]["data"]["terminal"] == "completed"
    assert events[0]["data"]["text"] == "答案"
    # A terminal snapshot closes the stream immediately.
    assert len(events) == 1


def test_stream_completed_snapshot_carries_reasoning(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    conv = _make_conversation(client, pid)
    _set_status(conv["id"], "busy")
    agent_env.broker.start(conv["id"], status="busy")
    agent_env.broker.reasoning(conv["id"], text="先想一步")
    agent_env.broker.complete(conv["id"], text="答案", status="active")

    resp = client.get(f"/api/agent/conversations/{conv['id']}/stream?project_id={pid}")
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert events[0]["event"] == "snapshot"
    assert events[0]["data"]["reasoning"] == "先想一步"
    # The final answer stays separate from the thinking process.
    assert events[0]["data"]["text"] == "答案"


def test_stream_failed_snapshot_carries_category(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    conv = _make_conversation(client, pid)
    _set_status(conv["id"], "busy")
    agent_env.broker.start(conv["id"], status="busy")
    agent_env.broker.fail(
        conv["id"], message="超时", status="active", category="timeout"
    )

    resp = client.get(f"/api/agent/conversations/{conv['id']}/stream?project_id={pid}")
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert events[0]["event"] == "snapshot"
    assert events[0]["data"]["terminal"] == "failed"
    assert events[0]["data"]["category"] == "timeout"


def test_stream_forwards_deltas_then_completes(agent_env):
    client = agent_env.client
    pid = _make_project(client)
    conv = _make_conversation(client, pid)
    _set_status(conv["id"], "busy")
    agent_env.broker.start(conv["id"], status="busy")

    poll = TaskStreamPoll(
        kind="events",
        events=(
            TaskStreamEvent(3, "delta", {"delta": "Hel"}),
            TaskStreamEvent(4, "delta", {"delta": "lo"}),
            TaskStreamEvent(
                5,
                "completed",
                {"status": "active", "message": "完成", "text": "Hello", "truncated": False},
            ),
        ),
    )
    agent_env.monkeypatch.setattr(
        agent_env.broker, "poll_after", lambda cid, sequence, *, stream_id=None: poll
    )

    resp = client.get(f"/api/agent/conversations/{conv['id']}/stream?project_id={pid}")
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    names = [event["event"] for event in events]
    assert names == ["snapshot", "delta", "delta", "completed"]
    assert events[1]["data"] == {"delta": "Hel"}
    assert events[2]["data"] == {"delta": "lo"}
    assert events[0]["id"] < events[1]["id"] < events[2]["id"] < events[3]["id"]
