"""Unit tests for the DSH agent gateway (line A).

Every test is offline: DSH is replaced by an httpx.MockTransport or, for the
pure SSE parser, a plain httpx.Response built from bytes.  Nothing here
touches a real network socket.
"""

import json

import httpx
import pytest

from app.services import agent_gateway
from app.services.agent_gateway import (
    AgentGatewayError,
    AuthCircuitBreaker,
    DshTurnStats,
    _category_message,
    _iter_prompt_events,
    _iter_raw_sse,
    conversation_workspace_dir,
    safe_agent_error,
)


@pytest.fixture(autouse=True)
def _reset_auth_circuit():
    """Keep the process-wide breaker from leaking state between tests."""

    agent_gateway.auth_circuit.reset()
    yield
    agent_gateway.auth_circuit.reset()


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _sse_response(body: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=body.encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )


# ---------------------------------------------------------------------------
# ensure_session: three-step provisioning
# ---------------------------------------------------------------------------


def test_ensure_session_provisions_workspace_session_permission(tmp_app_data):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        calls.append((request.method, request.url.path, payload))
        path = request.url.path
        if request.method == "POST" and path.endswith("/workspaces"):
            return httpx.Response(200, json={"ok": True, "data": {"id": "ws-1"}})
        if request.method == "POST" and path.endswith("/sessions"):
            return httpx.Response(200, json={"ok": True, "data": {"sessionId": "sess-1"}})
        if request.method == "PUT" and path.endswith("/permission"):
            return httpx.Response(200, json={"ok": True, "data": {}})
        return httpx.Response(404, json={"ok": False})

    handle = agent_gateway.gateway.ensure_session(
        conversation_id="conv-abc", title="我的对话", model="deepseek/deepseek-chat",
        transport=_transport(handler),
    )

    assert handle.session_id == "sess-1"
    assert handle.workspace_id == "ws-1"
    assert handle.cwd.endswith("conv-abc")

    assert [c[0] for c in calls] == ["POST", "POST", "PUT"]
    # Step 1: workspace path lives under the guarded workspace root.
    assert calls[0][2]["path"].endswith("conv-abc")
    # Step 2: session is bound to the workspace created in step 1.
    assert calls[1][2]["workspaceId"] == "ws-1"
    assert calls[1][2]["provider"] == "deepseek"
    assert calls[1][2]["model"] == "deepseek-chat"
    # Step 3: permission is downgraded to workspace-write on the new session.
    assert calls[2][2]["preset"] == "workspace-write"
    assert calls[2][1].endswith("/sessions/sess-1/permission")


def test_ensure_session_reuses_supplied_workspace_id(tmp_app_data):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/sessions"):
            return httpx.Response(200, json={"ok": True, "data": {"sessionId": "s"}})
        if request.method == "PUT":
            return httpx.Response(200, json={"ok": True, "data": {}})
        return httpx.Response(404, json={"ok": False})

    handle = agent_gateway.gateway.ensure_session(
        conversation_id="c1", workspace_id="ws-existing", transport=_transport(handler)
    )
    assert handle.workspace_id == "ws-existing"
    # No POST /workspaces when a workspace id is supplied.
    assert [c[0] for c in calls] == ["POST", "PUT"]


def test_ensure_session_permission_downgrade_failure_is_fatal(tmp_app_data):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/workspaces"):
            return httpx.Response(200, json={"ok": True, "data": {"id": "ws"}})
        if request.method == "POST" and path.endswith("/sessions"):
            return httpx.Response(200, json={"ok": True, "data": {"sessionId": "s"}})
        if request.method == "PUT":
            return httpx.Response(403, json={"ok": False})
        return httpx.Response(404, json={"ok": False})

    with pytest.raises(AgentGatewayError) as caught:
        agent_gateway.gateway.ensure_session(
            conversation_id="c1", transport=_transport(handler)
        )
    # The whole creation must abort rather than continue at full access.
    assert caught.value.category == "permission_denied"


def test_ensure_session_missing_workspace_id_is_bad_response(tmp_app_data):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/workspaces"):
            return httpx.Response(200, json={"ok": True, "data": {}})
        return httpx.Response(404, json={"ok": False})

    with pytest.raises(AgentGatewayError) as caught:
        agent_gateway.gateway.ensure_session(
            conversation_id="c1", transport=_transport(handler)
        )
    assert caught.value.category == "bad_response"


def test_create_session_includes_agent_preset_when_provided(tmp_app_data):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content or b"{}")
        return httpx.Response(200, json={"ok": True, "data": {"sessionId": "s"}})

    session_id = agent_gateway.gateway.create_session(
        workspace_id="ws-1",
        agent_preset="casegen-testcase",
        transport=_transport(handler),
    )
    assert session_id == "s"
    assert captured["body"]["agentPreset"] == "casegen-testcase"


def test_create_session_omits_agent_preset_when_empty(tmp_app_data):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content or b"{}")
        return httpx.Response(200, json={"ok": True, "data": {"sessionId": "s"}})

    agent_gateway.gateway.create_session(
        workspace_id="ws-1",
        transport=_transport(handler),
    )
    assert "agentPreset" not in captured["body"]

    agent_gateway.gateway.create_session(
        workspace_id="ws-1",
        agent_preset="",
        transport=_transport(handler),
    )
    assert "agentPreset" not in captured["body"]


# ---------------------------------------------------------------------------
# Workspace path guard
# ---------------------------------------------------------------------------


def test_conversation_workspace_dir_rejects_traversal(tmp_app_data):
    with pytest.raises(AgentGatewayError) as caught:
        conversation_workspace_dir("../evil")
    assert caught.value.category == "configuration"

    assert conversation_workspace_dir("safe-id").name == "safe-id"

    with pytest.raises(AgentGatewayError) as caught_empty:
        conversation_workspace_dir("")
    assert caught_empty.value.category == "bad_response"


# ---------------------------------------------------------------------------
# SSE parsing + filtering (_iter_prompt_events / _iter_raw_sse)
# ---------------------------------------------------------------------------


def _assemble(frames: list[tuple[str, str]]) -> str:
    return "".join(f"event: {name}\ndata: {data}\n\n" for name, data in frames)


def test_stream_events_are_sanitized_and_counted():
    body = _assemble([
        ("connected", json.dumps({"sessionId": "sess-x"})),
        ("reasoning", json.dumps({"text": "SECRET_THOUGHT"})),
        ("reasoning", json.dumps({"delta": "THINK2", "seq": 9})),
        ("delta", json.dumps({"delta": "Hel", "seq": 1})),
        ("delta", json.dumps({"delta": "lo", "seq": 2})),
        ("tool_call", json.dumps({"name": "read", "arguments": {"path": "secret.txt"}})),
        ("tool_call", json.dumps({"name": "unknown_tool", "arguments": {}})),
        ("tool_result", json.dumps({"content": "SECRET_RESULT"})),
        ("usage", json.dumps({"usage": {"inputTokens": 10, "outputTokens": 20, "totalTokens": 30}})),
        ("turn_end", json.dumps({"reason": {"kind": "completed"}})),
    ])
    stats = DshTurnStats()
    events = list(_iter_prompt_events(_sse_response(body), stats=stats, session_id="sess-x"))

    assert [e.kind for e in events] == [
        "connected", "reasoning", "reasoning", "delta", "delta",
        "tool_call", "tool_call", "completed"
    ]
    assert [e.text for e in events if e.kind == "reasoning"] == [
        "SECRET_THOUGHT", "THINK2"
    ]
    assert [e.seq for e in events if e.kind == "reasoning"] == [0, 9]
    assert [e.text for e in events if e.kind == "delta"] == ["Hel", "lo"]
    assert [e.seq for e in events if e.kind == "delta"] == [1, 2]
    # Every tool call is a boundary; only the whitelisted name carries copy.
    assert [e.text for e in events if e.kind == "tool_call"] == ["正在查阅资料…", ""]
    assert events[0].session_id == "sess-x"
    assert events[-1].reason == "completed"

    # Reasoning is intentionally surfaced (user-approved "思考过程"), but tool
    # results and tool arguments must still never leak downstream.
    leaked = "".join(e.text + e.reason for e in events if e.kind != "reasoning")
    for needle in ("SECRET_THOUGHT", "SECRET_RESULT", "secret.txt"):
        assert needle not in leaked

    assert stats.connected_frames == 1
    assert stats.reasoning_frames == 2
    assert stats.delta_frames == 2
    assert stats.delta_chars == 5
    assert stats.tool_call_frames == 2
    assert stats.tool_result_frames == 1
    assert stats.usage_frames == 1
    assert stats.input_tokens == 10
    assert stats.output_tokens == 20
    assert stats.total_tokens == 30


def test_run_code_tool_call_still_emits_boundary():
    # Real DSH tool names are outside the whitelist; they must still delimit
    # commentary from the final answer downstream.
    body = _assemble([
        ("tool_call", json.dumps({"name": "run_code", "arguments": {"code": "secret"}})),
        ("delta", json.dumps({"delta": "final", "seq": 1})),
        ("turn_end", json.dumps({"reason": {"kind": "completed"}})),
    ])
    stats = DshTurnStats()
    events = list(_iter_prompt_events(_sse_response(body), stats=stats, session_id="s"))

    assert [e.kind for e in events] == ["tool_call", "delta", "completed"]
    boundary = events[0]
    assert boundary.text == ""
    assert "run_code" not in json.dumps(boundary.as_dict())
    assert stats.tool_call_frames == 1


def test_done_literal_completes_stream():
    body = _assemble([
        ("delta", json.dumps({"delta": "hi"})),
        ("done", "[DONE]"),
    ])
    events = list(_iter_prompt_events(_sse_response(body), stats=DshTurnStats(), session_id="s"))
    assert events[-1].kind == "completed"
    assert events[-1].reason == "done"


def test_turn_end_cancel_reason_string_maps_to_cancelled():
    body = _assemble([("turn_end", json.dumps({"reason": "cancelled"}))])
    events = list(_iter_prompt_events(_sse_response(body), stats=DshTurnStats(), session_id="s"))
    assert len(events) == 1
    assert events[0].kind == "failed"
    assert events[0].category == "cancelled"


def test_turn_end_error_maps_to_stream_error():
    body = _assemble([("turn_end", json.dumps({"reason": {"kind": "error"}}))])
    events = list(_iter_prompt_events(_sse_response(body), stats=DshTurnStats(), session_id="s"))
    assert events[-1].kind == "failed"
    assert events[-1].category == "stream_error"


def test_ping_comments_are_counted_and_surfaced():
    body = ": ping\n\n" + _assemble([
        ("delta", json.dumps({"delta": "x"})),
        ("done", "[DONE]"),
    ])
    stats = DshTurnStats()
    events = list(_iter_prompt_events(_sse_response(body), stats=stats, session_id="s"))
    assert stats.ping_frames == 1
    assert "ping" in [e.kind for e in events]
    assert events[-1].kind == "completed"


def test_eof_without_terminal_is_stream_error():
    body = _assemble([("connected", json.dumps({"sessionId": "s"}))])
    events = list(_iter_prompt_events(_sse_response(body), stats=DshTurnStats(), session_id="s"))
    assert events[-1].kind == "failed"
    assert events[-1].category == "stream_error"


def test_iter_raw_sse_strips_field_space_and_ping():
    body = "event: delta\ndata:  {\"delta\": \"a b\"}\n\n: ping\n\n"
    stats = DshTurnStats()
    pairs = list(_iter_raw_sse(_sse_response(body), stats))
    # Only the single optional leading space is stripped per the SSE spec.
    assert pairs == [("delta", ' {"delta": "a b"}'), ("ping", "")]
    assert stats.ping_frames == 1


def test_should_stop_triggers_cancel_and_ends_iteration():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"ok": True, "data": {}})

    body = _assemble([("delta", json.dumps({"delta": "x"}))])
    events = list(_iter_prompt_events(
        _sse_response(body),
        stats=DshTurnStats(),
        session_id="sess-9",
        should_stop=lambda: True,
        transport=_transport(handler),
    ))
    # Cooperative cancel publishes no terminal event; the runner emits it.
    assert events == []
    assert any(method == "POST" and path.endswith("/sessions/sess-9/cancel")
               for method, path in seen)


# ---------------------------------------------------------------------------
# prompt_sync / cancel / delete_* best effort
# ---------------------------------------------------------------------------


def test_prompt_sync_returns_content_and_tool_calls_count():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content or b"{}")
        return httpx.Response(
            200, json={"ok": True, "data": {"content": "hi", "toolCalls": [{}, {}]}}
        )

    out = agent_gateway.gateway.prompt_sync(
        "sess-1", "do it", timeout_ms=1234, transport=_transport(handler)
    )
    assert out == {"content": "hi", "tool_calls_count": 2}
    assert captured["body"] == {"prompt": "do it", "timeout_ms": 1234}


def test_cancel_returns_true_on_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "data": {}})

    assert agent_gateway.gateway.cancel("sess-1", transport=_transport(handler)) is True


def test_delete_session_swallows_errors_and_returns_false():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(404, json={"ok": False})

    assert agent_gateway.gateway.delete_session("sess-1", transport=_transport(handler)) is False
    assert seen == ["DELETE"]


def test_delete_ids_empty_are_false_without_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not issue a request for an empty id")

    assert agent_gateway.gateway.delete_session("", transport=_transport(handler)) is False
    assert agent_gateway.gateway.delete_workspace("", transport=_transport(handler)) is False


# ---------------------------------------------------------------------------
# Error taxonomy helpers
# ---------------------------------------------------------------------------


def test_category_message_formats_and_suffixes():
    assert _category_message("http_error", 502).endswith("（HTTP 502）")
    assert _category_message("bad_response", 400).endswith("（HTTP 400）")
    # Only http_error / bad_response carry the status suffix.
    assert "HTTP" not in _category_message("unauthorized", 401)
    # Unknown categories fall back to the unknown copy.
    assert _category_message("definitely-not-a-category") == _category_message("unknown")


def test_agent_gateway_error_revalidates_fields():
    bad = AgentGatewayError(category="bogus", http_status=99, retryable="yes")
    assert bad.category == "unknown"
    assert bad.http_status is None
    assert bad.retryable is False
    assert str(bad) == _category_message("unknown")

    good = AgentGatewayError(category="timeout", http_status=504, retryable=True)
    assert good.category == "timeout"
    assert good.http_status == 504
    assert good.retryable is True
    assert str(good) == _category_message("timeout")


def test_safe_agent_error_exports_primitives_only():
    payload = safe_agent_error(
        AgentGatewayError(category="unauthorized", http_status=401, retryable=True)
    )
    assert payload == {
        "category": "unauthorized",
        "http_status": 401,
        "retryable": True,
        "error_type": "AgentGatewayError",
    }

    assert safe_agent_error(ValueError("boom")) == {
        "category": "unexpected_error",
        "error_type": "ValueError",
    }

    class WeirdError(Exception):
        pass

    assert safe_agent_error(WeirdError("x")) == {
        "category": "unexpected_error",
        "error_type": "Exception",
    }


# ---------------------------------------------------------------------------
# Auth circuit breaker
# ---------------------------------------------------------------------------


def test_auth_circuit_breaker_trips_after_threshold():
    breaker = AuthCircuitBreaker(threshold=2, cooldown_sec=300.0)
    assert breaker.allow_request() is True
    assert breaker.is_open is False
    assert breaker.record_failure() is False
    assert breaker.is_open is False
    assert breaker.record_failure() is True
    assert breaker.is_open is True
    assert breaker.allow_request() is False
    # A success closes it again.
    breaker.record_success()
    assert breaker.is_open is False
    assert breaker.allow_request() is True


def test_gateway_consecutive_401_opens_circuit(monkeypatch):
    monkeypatch.setattr(agent_gateway, "auth_circuit", AuthCircuitBreaker(threshold=2))
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(401, json={"ok": False})

    transport = _transport(handler)
    for _ in range(2):
        with pytest.raises(AgentGatewayError) as caught:
            agent_gateway.gateway.create_workspace("/tmp/ws", transport=transport)
        assert caught.value.category == "unauthorized"
    assert agent_gateway.auth_circuit.is_open is True

    before = hits["n"]
    with pytest.raises(AgentGatewayError) as caught_open:
        agent_gateway.gateway.create_workspace("/tmp/ws", transport=transport)
    assert caught_open.value.category == "unauthorized"
    # Suppressed before reaching the transport.
    assert hits["n"] == before


def test_stream_prompt_maps_transport_timeout_to_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    events = list(agent_gateway.gateway.stream_prompt(
        "sess-1", "p", transport=_transport(handler)
    ))
    assert len(events) == 1
    assert events[0].kind == "failed"
    assert events[0].category == "timeout"


def test_stream_prompt_maps_transport_error_to_stream_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    events = list(agent_gateway.gateway.stream_prompt(
        "sess-1", "p", transport=_transport(handler)
    ))
    assert events[-1].kind == "failed"
    assert events[-1].category == "stream_error"


# ---------------------------------------------------------------------------
# Knowledge-space prompt injection (offline, no DSH / no network)
# ---------------------------------------------------------------------------


def test_knowledge_space_dirs_returns_project_space_path(tmp_app_data):
    from pathlib import Path

    from sqlmodel import Session

    from app import config
    from app.db import get_engine, init_db
    from app.models.entities import AgentConversation, Project, WikiSpace
    from app.services import agent_turn

    init_db()
    with Session(get_engine()) as session:
        project = Project(name="KB Owner", slug="kb-owner", status="active")
        session.add(project)
        session.flush()
        space = WikiSpace(
            name="KB Alpha",
            slug="kb-alpha",
            scope="project",
            status="active",
            project_id=int(project.id),
        )
        session.add(space)
        session.flush()
        project.default_wiki_space_id = int(space.id)
        session.add(project)
        session.add(
            AgentConversation(
                id="conv-knowledge-1",
                project_id=str(project.id),
                title="知识会话",
            )
        )
        session.commit()
        project_id = str(project.id)

    dirs = agent_turn._knowledge_space_dirs(project_id)
    assert len(dirs) == 1
    root = Path(dirs[0])
    assert root.is_absolute()
    assert root == (Path(config.WIKI_DIR) / "spaces" / "kb-alpha").resolve()
    assert root.is_dir()
    assert (root / "pages").is_dir()


def test_knowledge_prompt_block_and_build_prompt_order(tmp_app_data):
    from app.services import agent_turn

    block = agent_turn._knowledge_prompt_block(["/tmp/kb-a", "/tmp/kb-b"])
    assert "/tmp/kb-a" in block
    assert "/tmp/kb-b" in block
    assert "只读" in block
    assert "glob / grep / read" in block
    assert "不要凭空编造" in block
    assert "只允许" in block
    assert "其他知识空间" in block
    assert "目录之外" in block

    prompt = agent_turn._build_prompt("帮我查规则", None, ["/tmp/kb-a"])
    assert "[知识空间]" in prompt
    assert "/tmp/kb-a" in prompt
    assert prompt.index("[知识空间]") < prompt.index("帮我查规则")

    # An empty user text still yields a non-empty prompt when a knowledge block
    # exists, so the turn body must guard on the raw user text (checked below).
    assert agent_turn._build_prompt("", None, ["/tmp/kb-a"]).strip() != ""
    assert agent_turn._build_prompt("", None, []) == ""
    assert agent_turn._build_prompt("x", None) == "x"


def test_knowledge_space_dirs_unknown_project_is_empty(tmp_app_data):
    from app.db import init_db
    from app.services import agent_turn

    init_db()
    assert agent_turn._knowledge_space_dirs("999999") == []
    assert agent_turn._knowledge_space_dirs("not-a-number") == []
