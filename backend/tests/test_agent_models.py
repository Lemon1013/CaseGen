"""Tests for the DSH-backed Agent model catalog, legacy value repair and
the no-longer-silent model-switch failures.

All DSH traffic is replaced by monkeypatched module-level gateway wrappers;
no test opens a real network socket.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.api import agent_chat
from app.db import get_engine, init_db, reset_engine
from app.main import create_app
from app.models.entities import AgentConversation, ModelConfig, Project
from app.services import agent_gateway
from app.services.agent_gateway import AgentGatewayError


@pytest.fixture
def env(tmp_app_data, monkeypatch):
    monkeypatch.setattr(config, "AGENT_ENABLED", True)
    client = TestClient(create_app())
    with Session(get_engine()) as session:
        pid = int(session.exec(select(Project)).first().id)
    return client, pid


DSH_PAYLOAD = {
    "defaultModel": {"provider": "magpie", "model": "workbuddy/hy4free", "reasoningEffort": "high"},
    "models": [
        {"id": "group/deepseek-flash", "provider": "magpie", "name": "Flash", "isDefault": False},
        {"id": "deepseek-flash", "provider": "deepseek-official", "name": "DS Flash", "isDefault": False},
        {"id": "workbuddy/hy4free", "provider": "magpie", "name": "Hy4", "isDefault": True},
    ],
}


def test_agent_models_proxies_dsh_catalog(env, monkeypatch):
    client, pid = env
    calls = []

    def fake_list_models(*, transport=None):
        calls.append(transport)
        return DSH_PAYLOAD

    monkeypatch.setattr(agent_gateway, "list_models", fake_list_models)
    resp = client.get(f"/api/agent/models?project_id={pid}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["default_provider"] == "magpie"
    assert data["default_model"] == "workbuddy/hy4free"
    assert data["default_reasoning_effort"] == "high"
    values = {item["value"] for item in data["models"]}
    assert "magpie/group/deepseek-flash" in values
    assert "deepseek-official/deepseek-flash" in values
    assert any(item["is_default"] for item in data["models"])
    assert calls == [None]


def test_agent_models_uses_mock_transport_offline(tmp_app_data, monkeypatch):
    """The gateway wrapper really goes through _request with a MockTransport."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "data": DSH_PAYLOAD})

    data = agent_gateway.list_models(transport=httpx.MockTransport(handler))
    assert data == DSH_PAYLOAD


def test_agent_models_surfaces_dsh_outage_as_502(env, monkeypatch):
    client, pid = env

    def boom(*, transport=None):
        raise AgentGatewayError(category="network_error")

    monkeypatch.setattr(agent_gateway, "list_models", boom)
    resp = client.get(f"/api/agent/models?project_id={pid}")
    assert resp.status_code == 502
    assert "DSH 模型列表" in resp.json()["detail"]


def test_agent_models_requires_project_id(env):
    client, _pid = env
    assert client.get("/api/agent/models").status_code == 422


def test_agent_models_disabled_when_agent_off(env, monkeypatch):
    client, pid = env
    monkeypatch.setattr(config, "AGENT_ENABLED", False)
    assert client.get(f"/api/agent/models?project_id={pid}").status_code == 410


def test_patch_conversation_clears_model_with_explicit_null(env, monkeypatch):
    client, pid = env
    created = client.post(
        f"/api/agent/conversations?project_id={pid}",
        json={"title": "t", "model": "magpie/group/deepseek-flash"},
    ).json()
    assert created["model"] == "magpie/group/deepseek-flash"
    switched = []
    monkeypatch.setattr(
        agent_gateway, "update_session_model",
        lambda sid, model, **kw: switched.append(model) or True,
    )
    patched = client.patch(
        f"/api/agent/conversations/{created['id']}?project_id={pid}",
        json={"model": None, "expected_version": created["version"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["model"] is None
    with Session(get_engine()) as session:
        assert session.get(AgentConversation, created["id"]).model is None


def test_patch_conversation_reports_failed_dsh_model_switch(env, monkeypatch):
    client, pid = env
    created = client.post(
        f"/api/agent/conversations?project_id={pid}",
        json={"title": "t"},
    ).json()
    with Session(get_engine()) as session:
        row = session.get(AgentConversation, created["id"])
        row.dsh_session_id = "sess-1"
        session.add(row)
        session.commit()
    monkeypatch.setattr(
        agent_gateway, "update_session_model", lambda sid, model, **kw: False
    )
    resp = client.patch(
        f"/api/agent/conversations/{created['id']}?project_id={pid}",
        json={"model": "deepseek-official/deepseek-flash"},
    )
    assert resp.status_code == 502
    assert "模型切换失败" in resp.json()["detail"]


def test_migration_repairs_only_recognised_legacy_values(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        session.add(ModelConfig(
            name="DEEPSEEK", base_url="http://x", api_key="k",
            model_name="group/deepseek-flash", is_default=True,
        ))
        session.add(AgentConversation(
            project_id="1", title="dirty", model="DEEPSEEK/group/deepseek-flash"
        ))
        session.add(AgentConversation(
            project_id="1", title="legal", model="magpie/group/deepseek-flash"
        ))
        session.add(AgentConversation(
            project_id="1", title="typed", model="CUSTOM/vendor/my-model"
        ))
        session.add(AgentConversation(
            project_id="1", title="two", model="deepseek-official/deepseek-flash"
        ))
        session.commit()
        ids = {
            row.title: str(row.id)
            for row in session.exec(select(AgentConversation)).all()
        }

    init_db()  # idempotent re-run, exactly like a second create_app
    init_db()
    with Session(get_engine()) as session:
        values = {row.title: row.model for row in session.exec(select(AgentConversation)).all()}
    assert values["dirty"] == "magpie/group/deepseek-flash"
    assert values["legal"] == "magpie/group/deepseek-flash"
    assert values["typed"] == "CUSTOM/vendor/my-model"
    assert values["two"] == "deepseek-official/deepseek-flash"
    assert len(ids) == 4


def test_migration_skips_when_no_local_model_name_matches(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        session.add(AgentConversation(
            project_id="1", title="unknown", model="UNKNOWN/group/deepseek-flash"
        ))
        session.commit()
    init_db()
    with Session(get_engine()) as session:
        row = session.exec(
            select(AgentConversation).where(AgentConversation.title == "unknown")
        ).one()
        assert row.model == "UNKNOWN/group/deepseek-flash"
