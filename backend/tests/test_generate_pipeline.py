import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_engine, init_db
from app.main import create_app
from app.models.entities import WikiPageRow


def _seed_wiki_page(title: str = "余额规则", content: str = "余额不足应拒绝下单。") -> None:
    """Insert a WikiPageRow + on-disk markdown so retrieve can score it."""
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
            "name": "gen-model",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test-generate",
            "model_name": "gpt-test",
            "is_default": True,
        },
    )
    assert r.status_code == 200
    return r.json()["id"]


def test_generate_creates_draft(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    _seed_wiki_page()
    mid = _create_model(client)

    fake_md = """# 用例：余额不足下单
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

    def fake_chat(**kwargs):
        messages = kwargs.get("messages") or []
        assert messages
        # Ensure wiki context / requirement reached the model.
        joined = "\n".join(m.get("content", "") for m in messages)
        assert "余额" in joined
        return fake_md

    monkeypatch.setattr("app.api.tasks._GENERATE_CHAT_FN", fake_chat)

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
    body = r.json()
    assert body["status"] == "draft"
    tid = body["id"]

    g = client.post(f"/api/tasks/{tid}/generate")
    assert g.status_code == 200
    gen = g.json()
    assert gen["status"] == "generated"
    assert gen["citation_count"] >= 1
    assert gen["latest_draft_snippet"]
    assert "用例" in gen["latest_draft_snippet"]

    detail = client.get(f"/api/tasks/{tid}").json()
    assert detail["status"] == "generated"
    assert detail["citation_count"] >= 1

    drafts = client.get(f"/api/tasks/{tid}/drafts").json()
    assert len(drafts) == 1
    assert "用例" in drafts[0]["content_md"]
    assert drafts[0]["version"] == 1

    citations = client.get(f"/api/tasks/{tid}/citations").json()
    assert len(citations) >= 1
    cite = citations[0]
    assert "id" in cite
    assert cite["title"]
    assert cite["path"]
    assert "score" in cite
    assert "snippet" in cite
    assert "wiki_page_id" in cite

    events = client.get(f"/api/tasks/{tid}/events").json()
    assert len(events) >= 2
    steps = {e["step"] for e in events}
    assert "retrieve" in steps
    assert "generate" in steps


def test_requirements_crud_minimal(tmp_app_data):
    client = TestClient(create_app())
    created = client.post(
        "/api/requirements",
        json={"title": "t1", "description": "d1", "focus_tags": ["a"]},
    )
    assert created.status_code == 200
    rid = created.json()["id"]
    assert created.json()["focus_tags"] == ["a"]

    listed = client.get("/api/requirements").json()
    assert any(item["id"] == rid for item in listed)

    got = client.get(f"/api/requirements/{rid}")
    assert got.status_code == 200
    assert got.json()["title"] == "t1"


def test_generate_fails_without_model(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    _seed_wiki_page()

    def fake_chat(**kwargs):
        return "# 用例：x\n"

    monkeypatch.setattr("app.api.tasks._GENERATE_CHAT_FN", fake_chat)

    r = client.post(
        "/api/tasks",
        json={"title": "余额不足", "description": "余额不足应失败"},
    )
    tid = r.json()["id"]
    g = client.post(f"/api/tasks/{tid}/generate")
    assert g.status_code == 200
    body = g.json()
    assert body["status"] == "failed"
    assert body["error_message"]


def test_generate_lean_fallback_on_primary_llm_error(tmp_app_data, monkeypatch):
    """Primary generate 502 must retry with lean system + still produce draft."""
    from app.services.llm import LLMError

    client = TestClient(create_app())
    _seed_wiki_page(
        title="集合竞价撤单",
        content="9:20-9:25 开盘集合竞价不接受撤单。",
    )
    mid = _create_model(client)
    calls = {"n": 0}
    systems: list[str] = []

    def flaky_chat(**kwargs):
        calls["n"] += 1
        messages = kwargs.get("messages") or []
        system = next(
            (m.get("content", "") for m in messages if m.get("role") == "system"),
            "",
        )
        systems.append(system)
        if calls["n"] == 1:
            raise LLMError(
                "LLM HTTP 502 (http://gpt.158918.xyz/v1/chat/completions): "
            )
        return (
            "# 用例：9:20-9:25 不可撤单\n"
            "- 优先级：P0\n- 类型：边界\n- 关联知识：[1]\n\n"
            "## 前置条件\n处于开盘集合竞价\n\n"
            "## 测试步骤\n1. 在 9:22 发起撤单\n\n"
            "## 预期结果\n撤单被拒绝\n"
        )

    monkeypatch.setattr("app.api.tasks._GENERATE_CHAT_FN", flaky_chat)

    tid = client.post(
        "/api/tasks",
        json={
            "title": "开盘集合竞价不可撤单",
            "description": "9:20-9:25 不接受撤单",
            "focus_tags": ["集合竞价", "撤单"],
            "model_id": mid,
        },
    ).json()["id"]

    g = client.post(f"/api/tasks/{tid}/generate")
    assert g.status_code == 200
    body = g.json()
    assert body["status"] == "generated", body
    assert calls["n"] == 2
    assert len(systems) == 2
    # lean fallback system is shorter / different from default seed prompt
    assert "测试专家" in systems[1] or "关联知识编号" in systems[1]
    assert systems[0] in systems[1]

    drafts = client.get(f"/api/tasks/{tid}/drafts").json()
    assert len(drafts) == 1
    assert "不可撤单" in drafts[0]["content_md"]
    assert "lean_fallback" in (drafts[0].get("prompt_version_ref") or "")

    events = client.get(f"/api/tasks/{tid}/events").json()
    msgs = " ".join(e["message"] for e in events)
    assert "精简上下文重试" in msgs


def test_strip_yaml_frontmatter_for_wiki_context():
    from app.services.task_pipeline import _strip_yaml_frontmatter, _truncate_wiki_context

    raw = "---\ntitle: t\ntype: business\n---\n正文规则：9:20不可撤单\n"
    assert _strip_yaml_frontmatter(raw).startswith("正文规则")
    assert "title:" not in _strip_yaml_frontmatter(raw)

    ctx = _truncate_wiki_context(
        [{"title": "t", "path": "p.md", "content": raw}],
        max_chars=500,
    )
    assert "正文规则" in ctx
    assert "type: business" not in ctx
