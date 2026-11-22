"""Phase 4 regression tests: config cleanup, external knowledge hook, and direct message_id TestCase import."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import get_engine
from app.main import create_app
from app.models.entities import (
    AgentConversation,
    AgentMessage,
    CaseDraft,
    GenerationTask,
    Project,
    Requirement,
    TestCase,
    TestCaseOperationLog,
)
from app.services import agent_draft, agent_turn
from app.services.agent_stream import AgentStreamBroker


@pytest.fixture
def phase4_env(tmp_app_data, monkeypatch):
    from app.api import agent_chat

    monkeypatch.setattr(agent_chat, "agent_stream", AgentStreamBroker())
    client = TestClient(create_app())
    return client


def _make_project(client: TestClient, slug: str) -> int:
    resp = client.post("/api/projects", json={"name": slug, "slug": slug})
    assert resp.status_code in (200, 201), resp.text
    return int(resp.json()["id"])


def _make_conversation(client: TestClient, project_id: int, title: str = "Phase 4 对话") -> str:
    resp = client.post(
        f"/api/agent/conversations?project_id={project_id}",
        json={"title": title},
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["id"])


def _make_requirement(client: TestClient, project_id: int, title: str = "既有需求") -> int:
    resp = client.post(
        f"/api/requirements?project_id={project_id}",
        json={"title": title, "description": "需求正文"},
    )
    assert resp.status_code in (200, 201), resp.text
    return int(resp.json()["id"])


def _insert_message(conversation_id: str, role: str, content: str) -> str:
    with Session(get_engine()) as session:
        row = AgentMessage(
            conversation_id=str(conversation_id),
            role=role,
            content=content,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return str(row.id)


def test_obsolete_pipeline_agent_config_removed():
    assert not hasattr(config, "PIPELINE_AGENT_STAGES")
    assert not hasattr(config, "AGENT_STAGE_MAX_CALLS")
    assert not hasattr(config, "agent_pipeline_stages")


def test_agent_turn_external_knowledge_hook_and_readonly_tool(tmp_app_data, monkeypatch):
    from app.services.knowledge_providers.base import (
        KnowledgeHit,
        ProjectKnowledgeSearchResult,
    )

    assert agent_turn.get_knowledge_context_hook() is None
    assert agent_turn._build_prompt("请设计登录测试", None) == "请设计登录测试"

    seen: dict[str, object] = {}

    def _custom_hook(project_id: int, query: str) -> str:
        seen["project_id"] = project_id
        seen["query"] = query
        return "命中规范：登录失败 5 次锁定 15 分钟。"

    agent_turn.set_knowledge_context_hook(_custom_hook)
    try:
        block = agent_turn._resolve_external_knowledge_block("7", "登录锁定")
        assert seen == {"project_id": 7, "query": "登录锁定"}
        assert "[外部知识库只读检索参考]" in block
        assert "[外部知识库只读检索参考结束]" in block

        prompt = agent_turn._build_prompt(
            "请设计登录测试",
            None,
            external_knowledge_block=block,
        )
        assert "登录失败 5 次锁定 15 分钟" in prompt
        assert prompt.endswith("请设计登录测试")

        def _broken_hook(_pid: int, _query: str) -> str:
            raise RuntimeError("boom")

        agent_turn.set_knowledge_context_hook(_broken_hook)
        assert agent_turn._resolve_external_knowledge_block("7", "登录锁定") == ""
    finally:
        agent_turn.set_knowledge_context_hook(None)

    captured_calls: list[tuple[int, str, int | None]] = []

    def _fake_search(session: Session, project_id: int, query: str, *, top_k=None):
        assert isinstance(session, Session)
        captured_calls.append((project_id, query, top_k))
        return ProjectKnowledgeSearchResult(
            configured=True,
            empty_reason="none",
            hits=[KnowledgeHit(title="登录规则", snippet="密码错误锁定")],
        )

    monkeypatch.setattr(agent_turn, "search_project_knowledge", _fake_search)
    tool = agent_turn.ProjectKnowledgeReadOnlyTool()
    result = tool.search(12, "密码规则", top_k=3)
    assert result.configured is True
    assert len(result.hits) == 1
    assert captured_calls == [(12, "密码规则", 3)]


def test_save_cases_by_specific_message_id_in_multiturn_conversation(phase4_env):
    client = phase4_env
    pid1 = _make_project(client, "proj-a")
    pid2 = _make_project(client, "proj-b")
    req1 = _make_requirement(client, pid1, "绑定目标需求")
    req2 = _make_requirement(client, pid2, "跨项目需求")
    conv_id = _make_conversation(client, pid1, "多轮测试设计")

    msg1_id = _insert_message(
        conv_id,
        "assistant",
        '第一轮：\n```json\n[{"case_key": "TC-HIST-001", "title": "历史气泡用例", "priority": "P0", "steps": [{"action": "步骤1", "expected": "预期1"}]}]\n```',
    )
    _insert_message(conv_id, "assistant", "第二轮纯文本说明，不包含任何用例 JSON。")
    _insert_message(
        conv_id,
        "assistant",
        '第三轮：\n```json\n[{"case_key": "TC-LATEST-001", "title": "最新气泡用例", "steps": [{"action": "步骤2", "expected": "预期2"}]}]\n```',
    )

    # Cross-project requirement must return 404.
    cross_resp = client.post(
        f"/api/agent/conversations/{conv_id}/save-cases?project_id={pid1}",
        json={
            "message_id": msg1_id,
            "target_mode": "existing_requirement",
            "requirement_id": req2,
        },
    )
    assert cross_resp.status_code == 404

    # Save explicitly by msg1_id (ignoring msg2 plain text and msg3 latest cases).
    resp = client.post(
        f"/api/agent/conversations/{conv_id}/save-cases?project_id={pid1}",
        json={
            "message_id": msg1_id,
            "target_mode": "existing_requirement",
            "requirement_id": req1,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["requirement_id"] == req1
    assert data["created_requirement"] is False
    assert data["message_id"] == msg1_id
    assert data["imported_count"] == 1
    assert data["case_keys"] == ["TC-HIST-001"]

    with Session(get_engine()) as session:
        assert session.exec(select(GenerationTask)).all() == []
        assert session.exec(select(CaseDraft)).all() == []
        cases = session.exec(select(TestCase).where(TestCase.requirement_id == req1)).all()
        assert [c.case_key for c in cases] == ["TC-HIST-001"]
        assert cases[0].source_task_id is None
        assert cases[0].source_draft_id is None
        assert cases[0].priority == "P0"


def test_save_cases_new_requirement_and_atomic_rollback(phase4_env, monkeypatch):
    client = phase4_env
    pid = _make_project(client, "proj-atomic")
    conv_id = _make_conversation(client, pid, "原子性测试")

    bad_msg_id = _insert_message(conv_id, "assistant", "没有 JSON 代码块的普通文本")
    good_msg_id = _insert_message(
        conv_id,
        "assistant",
        '```json\n[{"case_key": "TC-ATM-001", "title": "正常用例", "steps": [{"action": "点击提交", "expected": "成功"}]}]\n```',
    )

    # Empty title or missing JSON must leave Requirement count unchanged.
    resp_no_json = client.post(
        f"/api/agent/conversations/{conv_id}/save-cases?project_id={pid}",
        json={
            "message_id": bad_msg_id,
            "target_mode": "new_requirement",
            "requirement_title": "不应该残留的需求",
        },
    )
    assert resp_no_json.status_code == 422

    # Simulate failure inside import_cases_to_requirement after new Requirement is flushed.
    with monkeypatch.context() as mp:
        mp.setattr(
            agent_draft,
            "import_cases_to_requirement",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated write error")),
        )
        resp_err = client.post(
            f"/api/agent/conversations/{conv_id}/save-cases?project_id={pid}",
            json={
                "message_id": good_msg_id,
                "target_mode": "new_requirement",
                "requirement_title": "失败时应回滚的需求",
            },
        )
        assert resp_err.status_code == 422

    with Session(get_engine()) as session:
        assert session.exec(select(Requirement)).all() == []
        assert session.exec(select(TestCase)).all() == []

    resp_ok = client.post(
        f"/api/agent/conversations/{conv_id}/save-cases?project_id={pid}",
        json={
            "message_id": good_msg_id,
            "target_mode": "new_requirement",
            "requirement_title": "成功落库需求",
            "requirement_description": "来源摘要：上传附件 spec.md",
        },
    )
    assert resp_ok.status_code == 200, resp_ok.text
    body = resp_ok.json()
    assert body["created_requirement"] is True
    assert body["requirement_title"] == "成功落库需求"
    assert body["imported_count"] == 1


def test_save_cases_conflict_strategies_error_skip_overwrite(phase4_env):
    client = phase4_env
    pid = _make_project(client, "proj-conflict")
    req_id = _make_requirement(client, pid, "冲突策略需求")
    conv_id = _make_conversation(client, pid, "冲突测试")

    archived_at = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    with Session(get_engine()) as session:
        session.add(
            TestCase(
                requirement_id=req_id,
                case_key="TC-101",
                title="旧标题",
                content_md="## TC-101 旧标题\n\n### 测试步骤\n1. 旧步骤\n   预期结果：旧结果",
                priority="P2",
                status="archived",
                archived_at=archived_at,
                revision=1,
            )
        )
        session.commit()

    msg_id = _insert_message(
        conv_id,
        "assistant",
        '```json\n[\n'
        '  {"case_key": "TC-101", "title": "新标题", "priority": "P0", "steps": [{"action": "新步骤", "expected": "新结果"}]},\n'
        '  {"case_key": "TC-102", "title": "新增用例", "priority": "P1", "steps": [{"action": "步骤2", "expected": "结果2"}]}\n'
        ']\n```',
    )

    # 1. conflict_strategy="error" -> 409 with structured conflicts and zero writes (TC-102 not created).
    err_resp = client.post(
        f"/api/agent/conversations/{conv_id}/save-cases?project_id={pid}",
        json={
            "message_id": msg_id,
            "target_mode": "existing_requirement",
            "requirement_id": req_id,
            "conflict_strategy": "error",
        },
    )
    assert err_resp.status_code == 409, err_resp.text
    detail = err_resp.json()["detail"]
    assert detail["code"] == "conflict"
    assert len(detail["conflicts"]) == 1
    assert detail["conflicts"][0]["case_key"] == "TC-101"
    with Session(get_engine()) as session:
        keys = [c.case_key for c in session.exec(select(TestCase).where(TestCase.requirement_id == req_id)).all()]
        assert keys == ["TC-101"]

    # 2. conflict_strategy="skip" -> TC-101 preserved, TC-102 inserted.
    skip_resp = client.post(
        f"/api/agent/conversations/{conv_id}/save-cases?project_id={pid}",
        json={
            "message_id": msg_id,
            "target_mode": "existing_requirement",
            "requirement_id": req_id,
            "conflict_strategy": "skip",
        },
    )
    assert skip_resp.status_code == 200, skip_resp.text
    skip_body = skip_resp.json()
    assert skip_body["created_count"] == 1
    assert skip_body["overwritten_count"] == 0
    assert skip_body["skipped_count"] == 1
    assert skip_body["skipped_keys"] == ["TC-101"]
    assert skip_body["case_keys"] == ["TC-102"]

    with Session(get_engine()) as session:
        tc101 = session.exec(
            select(TestCase).where(TestCase.requirement_id == req_id, TestCase.case_key == "TC-101")
        ).one()
        assert tc101.title == "旧标题"
        assert tc101.revision == 1

    # 3. conflict_strategy="overwrite" -> TC-101 updated, revision incremented, archived status preserved, update log recorded.
    msg2_id = _insert_message(
        conv_id,
        "assistant",
        '```json\n[{"case_key": "TC-101", "title": "覆盖后标题", "priority": "P0", "steps": [{"action": "覆盖步骤", "expected": "覆盖结果"}]}]\n```',
    )
    ow_resp = client.post(
        f"/api/agent/conversations/{conv_id}/save-cases?project_id={pid}",
        json={
            "message_id": msg2_id,
            "target_mode": "existing_requirement",
            "requirement_id": req_id,
            "conflict_strategy": "overwrite",
        },
    )
    assert ow_resp.status_code == 200, ow_resp.text
    ow_body = ow_resp.json()
    assert ow_body["overwritten_count"] == 1
    assert ow_body["overwritten_keys"] == ["TC-101"]

    with Session(get_engine()) as session:
        tc101 = session.exec(
            select(TestCase).where(TestCase.requirement_id == req_id, TestCase.case_key == "TC-101")
        ).one()
        assert tc101.title == "覆盖后标题"
        assert tc101.priority == "P0"
        assert tc101.revision == 2
        assert tc101.status == "archived"
        assert tc101.archived_at == archived_at
        logs = session.exec(
            select(TestCaseOperationLog).where(
                TestCaseOperationLog.test_case_id == tc101.id,
                TestCaseOperationLog.operation == "update",
            )
        ).all()
        assert len(logs) == 1


def test_save_cases_rejects_user_role_cross_conversation_and_renders_pending_items(phase4_env):
    client = phase4_env
    pid = _make_project(client, "proj-guards")
    conv_a = _make_conversation(client, pid, "会话 A")
    conv_b = _make_conversation(client, pid, "会话 B")

    user_msg_id = _insert_message(conv_a, "user", "请生成用例")
    other_msg_id = _insert_message(
        conv_b,
        "assistant",
        '```json\n[{"case_key": "TC-B-001", "title": "B会话用例", "steps": [{"action": "a", "expected": "b"}]}]\n```',
    )

    resp_user = client.post(
        f"/api/agent/conversations/{conv_a}/save-cases?project_id={pid}",
        json={
            "message_id": user_msg_id,
            "target_mode": "new_requirement",
            "requirement_title": "非法角色",
        },
    )
    assert resp_user.status_code == 422

    resp_cross = client.post(
        f"/api/agent/conversations/{conv_a}/save-cases?project_id={pid}",
        json={
            "message_id": other_msg_id,
            "target_mode": "new_requirement",
            "requirement_title": "跨会话窃取",
        },
    )
    assert resp_cross.status_code == 404
    assert resp_cross.json()["detail"] == "Resource not found in project"

    # Verify _render_case flattens pending_items instead of emitting Python list repr.
    groups = agent_draft.parse_agent_case_groups(
        '```json\n[{"case_key": "TC-P-001", "title": "待确认项渲染", "pending_items": ["第一条\\n第二条", "第三条"], "steps": [{"action": "x", "expected": "y"}]}]\n```'
    )
    rendered_md, _ = agent_draft.render_draft_markdown(groups)
    assert "- 第一条" in rendered_md
    assert "- 第二条" in rendered_md
    assert "- 第三条" in rendered_md
    assert "['" not in rendered_md
