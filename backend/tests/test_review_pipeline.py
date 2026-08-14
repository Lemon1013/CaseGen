import json
import time

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import get_engine, init_db
from app.main import create_app
from app.models.entities import (
    CaseDraft,
    GenerationTask,
    PromptRevision,
    Requirement,
    TaskCitation,
    WikiPageRow,
)
from app.services.task_pipeline import _build_review_messages


def _seed_wiki_page(title: str = "余额规则", content: str = "余额不足应拒绝下单。") -> None:
    init_db()
    from app import config

    config.WIKI_PAGES_DIR.mkdir(parents=True, exist_ok=True)
    rel_path = f"pages/{title}.md"
    file_path = config.WIKI_DIR / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        f"---\ntitle: {title}\ntype: business\ntags: [\"余额\", \"下单\"]\n---\n{content}\n",
        encoding="utf-8",
    )
    with Session(get_engine()) as session:
        session.add(
            WikiPageRow(
                path=rel_path,
                title=title,
                page_type="business",
                tags_json=json.dumps(["余额", "下单"], ensure_ascii=False),
            )
        )
        session.commit()


def _create_model(client: TestClient) -> int:
    r = client.post(
        "/api/models",
        json={
            "name": "review-model",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test-review",
            "model_name": "gpt-test",
            "is_default": True,
        },
    )
    assert r.status_code == 200
    return r.json()["id"]


def _generate_with_confirmation(client: TestClient, task_id: int):
    response = client.post(f"/api/tasks/{task_id}/generate")
    assert response.status_code == 200
    if response.json()["status"] == "awaiting_confirmation":
        cp = client.get(f"/api/tasks/{task_id}/retrieval-checkpoint").json()
        response = client.post(
            f"/api/tasks/{task_id}/retrieval-checkpoint/confirm",
            json={
                "selected_citation_ids": [item["id"] for item in cp["candidate_citations"]],
                "supplemental_text": "基于确认检索结果",
                "expected_version": cp["version"],
                "idempotency_key": f"review-test-{task_id}-{cp['version']}",
            },
        )
        assert response.status_code == 200
    for _ in range(80):
        current = client.get(f"/api/tasks/{task_id}").json()
        if current["status"] not in {"retrieving", "generating"}:
            return current
        time.sleep(0.05)
    return current


def test_review_optimize_apply_regenerate_finalize(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    _seed_wiki_page()
    mid = _create_model(client)

    draft_v1 = """# 用例：余额不足下单
- 优先级：P0
- 类型：异常
- 关联知识：[1]

## 前置条件
账户可用余额为 0

## 测试步骤
1. 提交限价买单

## 预期结果
下单被拒绝，提示余额不足
"""
    draft_v2 = draft_v1.replace("v1", "v2") + "\n## 数据与环境备注\n覆盖精度边界\n"
    review_json = json.dumps(
        {
            "score": 86,
            "verdict": "pass",
            "issues": ["步骤可更细"],
            "missing_scenarios": [],
            "prompt_improvement_hints": ["强制要求覆盖金额精度"],
            "ready_for_final": True,
        },
        ensure_ascii=False,
    )
    optimized_prompt = "# 优化后 generate 提示词\n必须覆盖余额不足与精度边界。\n"

    call_count = {"n": 0}

    def fake_chat(**kwargs):
        messages = kwargs.get("messages") or []
        joined = "\n".join(m.get("content", "") for m in messages)
        call_count["n"] += 1
        # Review system prompt asks for JSON only; optimize asks for full prompt body.
        if "只返回 **一个 JSON 对象**" in joined or "金融/交易所测试评审专家" in joined:
            return review_json
        if "提示词工程师" in joined or "重写一版更优" in joined or "当前 generate 提示词" in joined:
            return optimized_prompt
        # generate / regenerate
        if call_count["n"] >= 4 or "draft v2" in joined:
            return draft_v2
        # First generate call(s)
        if "生成测试用例" in joined or "Wiki 引用上下文" in joined or "需求" in joined:
            # After temp prompt applied, system content should include optimized text.
            system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
            if "必须覆盖余额不足与精度边界" in system:
                return draft_v2
            return draft_v1
        return draft_v1

    monkeypatch.setattr("app.api.tasks._PIPELINE_CHAT_FN", fake_chat)
    monkeypatch.setattr("app.api.tasks._GENERATE_CHAT_FN", fake_chat)
    monkeypatch.setattr("app.api.tasks._REVIEW_CHAT_FN", fake_chat)
    monkeypatch.setattr("app.api.tasks._OPTIMIZE_CHAT_FN", fake_chat)
    monkeypatch.setattr("app.services.task_pipeline._PIPELINE_CHAT_FN", fake_chat)
    monkeypatch.setattr("app.services.task_pipeline._GENERATE_CHAT_FN", fake_chat)
    monkeypatch.setattr("app.services.task_pipeline._REVIEW_CHAT_FN", fake_chat)
    monkeypatch.setattr("app.services.task_pipeline._OPTIMIZE_CHAT_FN", fake_chat)

    r = client.post(
        "/api/tasks",
        json={
            "title": "余额不足下单",
            "description": "现货限价单余额不足应失败",
            "focus_tags": ["余额"],
            "model_id": mid,
        },
    )
    assert r.status_code == 200
    tid = r.json()["id"]

    generated = _generate_with_confirmation(client, tid)
    assert generated["status"] == "generated"
    assert generated["latest_draft_version"] == 1

    rev = client.post(f"/api/tasks/{tid}/review")
    assert rev.status_code == 200
    body = rev.json()
    assert body["status"] == "reviewed"
    assert body["latest_review"] is not None
    assert body["latest_review"]["score"] == 86
    assert body["latest_review"]["verdict"] == "pass"
    assert body["latest_review"]["payload"]["ready_for_final"] is True

    detail = client.get(f"/api/tasks/{tid}").json()
    assert detail["latest_review"]["score"] == 86

    opt = client.post(f"/api/tasks/{tid}/optimize-prompt")
    assert opt.status_code == 200
    assert opt.json()["status"] == "reviewed"

    revisions = client.get(f"/api/tasks/{tid}/revisions").json()
    assert len(revisions) >= 1
    revision_id = revisions[0]["id"]
    assert revisions[0]["status"] == "pending"
    assert "必须覆盖余额不足与精度边界" in revisions[0]["new_content"]

    applied = client.post(
        f"/api/tasks/{tid}/apply-prompt",
        json={"revision_id": revision_id, "mode": "task_temp"},
    )
    assert applied.status_code == 200

    with Session(get_engine()) as session:
        task = session.get(GenerationTask, tid)
        assert task is not None
        assert task.temp_prompt_content is not None
        assert "必须覆盖余额不足与精度边界" in task.temp_prompt_content
        rev_row = session.get(PromptRevision, revision_id)
        assert rev_row is not None
        assert rev_row.status == "applied_task_temp"

    regen = client.post(f"/api/tasks/{tid}/regenerate")
    assert regen.status_code == 200
    regen_body = regen.json()
    if regen_body["status"] == "awaiting_confirmation":
        cp = client.get(f"/api/tasks/{tid}/retrieval-checkpoint").json()
        regen = client.post(
            f"/api/tasks/{tid}/retrieval-checkpoint/confirm",
            json={"selected_citation_ids": [x["id"] for x in cp["candidate_citations"]], "supplemental_text": "重生成确认", "expected_version": cp["version"], "idempotency_key": f"regen-{tid}-{cp['version']}"},
        )
        assert regen.status_code == 200
    for _ in range(80):
        regen_body = client.get(f"/api/tasks/{tid}").json()
        if regen_body["status"] not in {"retrieving", "generating"}:
            break
        time.sleep(0.05)
    assert regen_body["status"] == "generated"
    assert regen_body["latest_draft_version"] == 2

    drafts = client.get(f"/api/tasks/{tid}/drafts").json()
    assert len(drafts) == 2
    versions = sorted(d["version"] for d in drafts)
    assert versions == [1, 2]

    # Need reviewed status for finalize from reviewed path; re-review after regen.
    rev2 = client.post(f"/api/tasks/{tid}/review")
    assert rev2.status_code == 200
    assert rev2.json()["status"] == "reviewed"

    fin = client.post(f"/api/tasks/{tid}/finalize")
    assert fin.status_code == 200
    assert fin.json()["status"] == "finalized"

    events = client.get(f"/api/tasks/{tid}/events").json()
    steps = {e["step"] for e in events}
    assert "review" in steps
    assert "optimize" in steps
    assert "apply_prompt" in steps
    assert "regenerate" in steps
    assert "finalize" in steps


def test_finalize_from_generated(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    _seed_wiki_page()
    mid = _create_model(client)

    def fake_chat(**kwargs):
        return "# 用例：x\n- 优先级：P1\n"

    monkeypatch.setattr("app.api.tasks._GENERATE_CHAT_FN", fake_chat)
    monkeypatch.setattr("app.services.task_pipeline._GENERATE_CHAT_FN", fake_chat)

    tid = client.post(
        "/api/tasks",
        json={"title": "t", "description": "d", "model_id": mid},
    ).json()["id"]
    assert _generate_with_confirmation(client, tid)["status"] == "generated"
    fin = client.post(f"/api/tasks/{tid}/finalize")
    assert fin.status_code == 200
    assert fin.json()["status"] == "finalized"


def test_finalize_without_draft_fails(tmp_app_data):
    client = TestClient(create_app())
    tid = client.post(
        "/api/tasks",
        json={"title": "t", "description": "d"},
    ).json()["id"]
    fin = client.post(f"/api/tasks/{tid}/finalize")
    assert fin.status_code == 400


def test_apply_prompt_accepts_manual_edit_before_confirmation(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        requirement = Requirement(title="t", description="d")
        session.add(requirement)
        session.flush()
        task = GenerationTask(requirement_id=int(requirement.id), status="failed")
        session.add(task)
        session.flush()
        revision = PromptRevision(
            task_id=int(task.id),
            new_content="模型生成的版本",
            status="pending",
        )
        session.add(revision)
        session.commit()
        task_id = int(task.id)
        revision_id = int(revision.id)

    client = TestClient(create_app())
    applied = client.post(
        f"/api/tasks/{task_id}/apply-prompt",
        json={
            "revision_id": revision_id,
            "mode": "task_temp",
            "content": "人工确认后的提示词\n保留关键约束。",
        },
    )
    assert applied.status_code == 200
    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        revision = session.get(PromptRevision, revision_id)
        assert task is not None and task.temp_prompt_content == "人工确认后的提示词\n保留关键约束。"
        assert revision is not None and revision.new_content == task.temp_prompt_content

    repeated = client.post(
        f"/api/tasks/{task_id}/apply-prompt",
        json={"revision_id": revision_id, "mode": "task_temp"},
    )
    assert repeated.status_code == 400


def test_review_messages_preserve_wiki_and_source_labels():
    requirement = Requirement(
        title="集合竞价撤单",
        description="核验不可撤单时段",
        focus_tags_json='["撤单"]',
    )
    draft = CaseDraft(task_id=1, content_md="## TC-001\n关联知识：[1] [S1]")
    citations = [
        TaskCitation(
            task_id=1,
            citation_type="wiki",
            wiki_page_id=7,
            title="撤单规则",
            path="pages/rule.cancel.md",
            snippet="规则摘要",
        ),
        TaskCitation(
            task_id=1,
            citation_type="source",
            source_chunk_id=12,
            title="交易规则原文",
            path="documents/1/chunks/12",
            snippet="短摘要",
            content_excerpt="9:20 至 9:25 不接受撤单申报。",
            clause_ids_json='["3.5.2"]',
            anchor_clause="3.5.2",
        ),
    ]

    joined = "\n".join(
        item["content"]
        for item in _build_review_messages("评审系统", requirement, draft, citations)
    )
    assert "[1] 撤单规则" in joined
    assert "[S1] 交易规则原文" in joined
    assert "source_chunk_id=12" in joined
    assert "锚定条款=3.5.2" in joined
    assert "9:20 至 9:25 不接受撤单申报" in joined
    assert "终版分数门槛" in joined
