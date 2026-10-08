import json
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import get_engine
from app.main import create_app
from app.models.entities import (
    CaseDraft,
    DraftTestPointLink,
    GenerationTask,
    Requirement,
    TaskCitation,
    TaskRetrievalCheckpoint,
    TaskTestPointCheckpoint,
    TestPoint,
    TestPointCitation,
)
from app.services.task_pipeline import run_generate
from app.services.task_stream import task_stream


def _create_model(client: TestClient) -> int:
    response = client.post(
        "/api/models",
        json={
            "name": "pbp-test-model",
            "base_url": "https://example.test/v1",
            "api_key": "sk-test-pbp",
            "model_name": "fake-chat",
            "is_default": True,
        },
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_point_by_point_pipeline_execution_and_links(tmp_app_data, monkeypatch):
    """Test point-by-point generation pipeline, stream notifications, and draft_test_point_links."""
    client = TestClient(create_app())
    mid = _create_model(client)

    # 1. 创建任务与需求
    with Session(get_engine()) as session:
        req = Requirement(title="转账校验需求", description="支持普通转账与大额转账的风控校验")
        session.add(req)
        session.commit()
        session.refresh(req)

        task = GenerationTask(
            requirement_id=req.id,
            status="generating",
            model_id=mid,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        task_id = int(task.id)

        # 添加 citation
        citation = TaskCitation(
            task_id=task_id,
            citation_type="wiki",
            title="转账风控规则",
            path="pages/transfer_risk.md",
            snippet="单笔超过 50,000 元需要风控审批。",
            score=0.95,
        )
        session.add(citation)
        session.commit()
        session.refresh(citation)

        # 添加检索检查点
        rc = TaskRetrievalCheckpoint(
            task_id=task_id,
            attempt=1,
            status="confirmed",
            query="转账风控",
            retrieval_json=json.dumps({
                "context": {
                    "citations": [{"task_citation_id": citation.id, "title": "转账风控规则", "path": "pages/transfer_risk.md"}],
                    "wiki_hits": [],
                    "source_hits": [],
                }
            }),
            selected_citation_ids_json=json.dumps([citation.id]),
        )
        session.add(rc)
        session.commit()
        session.refresh(rc)

        # 添加测试点检查点与 2 个已确认测试点
        tpc = TaskTestPointCheckpoint(
            task_id=task_id,
            retrieval_checkpoint_id=rc.id,
            attempt=1,
            status="confirmed",
            version=1,
        )
        session.add(tpc)
        session.commit()
        session.refresh(tpc)

        tp1 = TestPoint(
            task_id=task_id,
            checkpoint_id=tpc.id,
            stable_key="TP-001",
            title="普通转账成功流程",
            verification_goal="验证余额充足且未超限时转账立即成功",
            dimension="positive",
            priority="P0",
            sort_order=0,
            is_selected=True,
            is_excluded=False,
        )
        tp2 = TestPoint(
            task_id=task_id,
            checkpoint_id=tpc.id,
            stable_key="TP-002",
            title="大额转账触发风控审批",
            verification_goal="验证超过50000元转账进入待审批状态",
            dimension="boundary",
            priority="P1",
            sort_order=1,
            is_selected=True,
            is_excluded=False,
        )
        session.add(tp1)
        session.add(tp2)
        session.commit()
        session.refresh(tp1)
        session.refresh(tp2)
        tp1_id = int(tp1.id)
        tp2_id = int(tp2.id)

        # 关联 citation
        session.add(TestPointCitation(test_point_id=tp2_id, citation_id=int(citation.id)))
        session.commit()

    # 2. 模拟逐测试点生成的 LLM 调用
    calls: list[dict] = []

    def mock_pbp_chat(**kwargs):
        messages = kwargs.get("messages") or []
        user_content = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        calls.append({"messages": messages, "content": user_content})

        if "TP-001" in user_content:
            return (
                "## TC-001 普通转账成功\n"
                "- 关联测试点：TP-001\n"
                "- 优先级：P0\n"
                "- 类型：正向流程\n"
                "- 验证目标：验证余额充足且未超限时转账立即成功\n\n"
                "### 前置条件\n"
                "账户A可用余额为10,000元，账户B正常\n\n"
                "### 测试数据\n"
                "转账金额：1,000元\n\n"
                "### 测试步骤\n"
                "1. 输入转账金额 1000 元并点击提交\n"
                "   预期结果：提示转账成功\n"
                "2. 查询账户 A 和账户 B 余额\n"
                "   预期结果：账户 A 扣减 1000 元，账户 B 增加 1000 元\n"
            )
        else:
            return (
                "## TC-002 大额转账触发风控\n"
                "- 关联测试点：TP-002\n"
                "- 优先级：P1\n"
                "- 类型：边界场景\n"
                "- 验证目标：验证超过50000元转账进入待审批状态\n\n"
                "### 前置条件\n"
                "账户A可用余额为100,000元\n\n"
                "### 测试数据\n"
                "转账金额：50,001元\n\n"
                "### 测试步骤\n"
                "1. 输入转账金额 50001 元并点击提交\n"
                "   预期结果：提示订单已进入风控人工审批流程\n"
            )

    mock_pbp_chat.point_by_point = True

    # 3. 运行 run_generate (指定 point_by_point=True)
    with Session(get_engine()) as session:
        gen_task = run_generate(session, task_id, chat_fn=mock_pbp_chat, point_by_point=True)
        assert gen_task.status == "generated", f"Failed with: {gen_task.error_message}"

    # 4. 验证调用次数为 2 次（逐点生成各调用一次）
    assert len(calls) == 2
    assert "TP-001" in calls[0]["content"]
    assert "TP-002" in calls[1]["content"]
    assert "生成粒度" in calls[0]["content"]
    assert "用例数量规范" in calls[0]["content"]

    # 5. 验证 SSE stream 状态记录了逐点进度
    snapshot = task_stream.snapshot(task_id)
    assert snapshot is not None
    assert snapshot["terminal"] == "completed"

    # 6. 验证数据库中 DraftTestPointLink 写入
    with Session(get_engine()) as session:
        drafts = session.exec(select(CaseDraft).where(CaseDraft.task_id == task_id)).all()
        assert len(drafts) == 1
        draft = drafts[0]
        assert "TC-001" in draft.content_md
        assert "TC-002" in draft.content_md

        links = session.exec(select(DraftTestPointLink).where(DraftTestPointLink.draft_id == draft.id)).all()
        assert len(links) == 2
        link_pairs = {(l.case_key, l.test_point_id) for l in links}
        assert ("TC-001", tp1_id) in link_pairs
        assert ("TC-002", tp2_id) in link_pairs

    # 7. 验证 GET /api/tasks/{task_id}/drafts 返回按测试点聚合的结构化数据
    resp = client.get(f"/api/tasks/{task_id}/drafts")
    assert resp.status_code == 200
    draft_data = resp.json()
    assert len(draft_data) == 1
    p_with_c = draft_data[0].get("points_with_cases")
    assert p_with_c is not None
    assert len(p_with_c) == 2

    group1 = next((g for g in p_with_c if g["stable_key"] == "TP-001"), None)
    assert group1 is not None
    assert group1["title"] == "普通转账成功流程"
    assert len(group1["cases"]) == 1
    c1 = group1["cases"][0]
    assert c1["case_key"] == "TC-001"
    assert c1["priority"] == "P0"
    assert c1["type"] == "正向流程"
    assert len(c1["steps"]) == 2
    assert c1["steps"][0]["step_no"] == 1
    assert "提示转账成功" in c1["steps"][0]["expected"]

    group2 = next((g for g in p_with_c if g["stable_key"] == "TP-002"), None)
    assert group2 is not None
    assert group2["title"] == "大额转账触发风控审批"
    assert len(group2["cases"]) == 1
    c2 = group2["cases"][0]
    assert c2["case_key"] == "TC-002"
    assert c2["priority"] == "P1"
    assert c2["type"] == "边界场景"
    assert len(c2["steps"]) == 1
    assert "风控人工审批" in c2["steps"][0]["expected"]


def test_point_by_point_fallback_order_linking(tmp_app_data, monkeypatch):
    """Test linking fallback when LLM output lacks explicit test_point_keys."""
    client = TestClient(create_app())
    mid = _create_model(client)

    with Session(get_engine()) as session:
        req = Requirement(title="转账校验需求2", description="测试顺序回退关联")
        session.add(req)
        session.commit()
        session.refresh(req)

        task = GenerationTask(
            requirement_id=req.id,
            status="generating",
            model_id=mid,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        task_id = int(task.id)

        rc = TaskRetrievalCheckpoint(
            task_id=task_id,
            attempt=1,
            status="confirmed",
            query="转账风控",
            retrieval_json=json.dumps({"context": {"citations": [], "wiki_hits": [], "source_hits": []}}),
            selected_citation_ids_json="[]",
        )
        session.add(rc)
        session.commit()
        session.refresh(rc)

        tpc = TaskTestPointCheckpoint(
            task_id=task_id,
            retrieval_checkpoint_id=rc.id,
            attempt=1,
            status="confirmed",
            version=1,
        )
        session.add(tpc)
        session.commit()
        session.refresh(tpc)

        tp1 = TestPoint(
            task_id=task_id,
            checkpoint_id=tpc.id,
            stable_key="TP-101",
            title="测试点A",
            verification_goal="验证点A",
            dimension="positive",
            priority="P0",
            sort_order=0,
            is_selected=True,
            is_excluded=False,
        )
        session.add(tp1)
        session.commit()
        session.refresh(tp1)
        tp1_id = int(tp1.id)

    # 模拟未写关联测试点 key 的输出
    def mock_chat(**kwargs):
        return (
            "## TC-101 用例A\n"
            "- 优先级：P0\n\n"
            "### 前置条件\n"
            "系统就绪\n\n"
            "### 测试步骤\n"
            "1. 执行步骤一\n"
            "   预期结果：步骤一通过\n"
        )

    mock_chat.point_by_point = True

    with Session(get_engine()) as session:
        gen_task = run_generate(session, task_id, chat_fn=mock_chat, point_by_point=True)
        assert gen_task.status == "generated"

        links = session.exec(select(DraftTestPointLink)).all()
        matching = [l for l in links if l.case_key == "TC-101"]
        assert len(matching) == 1
        assert matching[0].test_point_id == tp1_id

    resp = client.get(f"/api/tasks/{task_id}/drafts")
    assert resp.status_code == 200
    p_with_c = resp.json()[0]["points_with_cases"]
    assert len(p_with_c) == 1
    assert p_with_c[0]["cases"][0]["case_key"] == "TC-101"
