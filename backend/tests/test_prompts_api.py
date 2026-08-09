from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import get_engine
from app.main import create_app
from app.models.entities import PromptTemplate
from app.services import prompts_seed
from app.services.prompts_seed import BUNDLED_PROMPT_VERSION, seed_default_prompts


def test_multiple_active_prompts_per_type(tmp_app_data):
    client = TestClient(create_app())
    r1 = client.post(
        "/api/prompts",
        json={"name": "g1", "type": "generate", "content": "A", "is_active": True},
    )
    r2 = client.post(
        "/api/prompts",
        json={"name": "g2", "type": "generate", "content": "B", "is_active": True},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    items = client.get("/api/prompts", params={"type": "generate"}).json()
    actives = [p for p in items if p["is_active"]]
    active_contents = {p["content"] for p in actives}
    assert {"A", "B"}.issubset(active_contents)


def test_default_prompts_seeded(tmp_app_data):
    client = TestClient(create_app())
    items = client.get("/api/prompts").json()
    types = {p["type"] for p in items if p["is_active"]}
    assert {
        "generate",
        "review",
        "optimize",
        "wiki_analyze",
        "wiki_write",
    }.issubset(types)
    assert all(
        p["version"] >= BUNDLED_PROMPT_VERSION
        for p in items
        if p["is_active"] and p["name"].startswith("default_")
    )


def test_seed_keeps_custom_active_prompt(tmp_app_data):
    client = TestClient(create_app())
    created = client.post(
        "/api/prompts",
        json={
            "name": "team-generate",
            "type": "generate",
            "content": "团队自定义提示词",
            "is_active": True,
        },
    ).json()

    with Session(get_engine()) as session:
        seed_default_prompts(session)
        active = session.exec(
            select(PromptTemplate).where(
                PromptTemplate.type == "generate",
                PromptTemplate.is_active == True,  # noqa: E712
            ).order_by(PromptTemplate.id)
        ).all()

    custom = next(item for item in active if item.id == created["id"])
    assert custom.content == "团队自定义提示词"
    assert any(item.name == "default_generate" for item in active)


def test_seed_upgrades_recognized_bundled_prompt(tmp_app_data, monkeypatch):
    TestClient(create_app())
    legacy_content = "已发布的旧版内置提示词"
    with Session(get_engine()) as session:
        for row in session.exec(
            select(PromptTemplate).where(PromptTemplate.type == "generate")
        ).all():
            row.is_active = False
            session.add(row)
        legacy = PromptTemplate(
            name="default_generate",
            type="generate",
            content=legacy_content,
            version=1,
            is_active=True,
        )
        session.add(legacy)
        session.commit()
        session.refresh(legacy)
        legacy_id = legacy.id

        monkeypatch.setitem(
            prompts_seed._LEGACY_DEFAULT_HASHES,
            "generate",
            frozenset({prompts_seed._content_hash(legacy_content)}),
        )
        seed_default_prompts(session)
        active = session.exec(
            select(PromptTemplate).where(
                PromptTemplate.type == "generate",
                PromptTemplate.is_active == True,  # noqa: E712
            )
        ).one()
        old = session.get(PromptTemplate, legacy_id)

    assert active.id != legacy_id
    assert active.version >= BUNDLED_PROMPT_VERSION
    assert "# 证据规则" in active.content
    assert old is not None and old.is_active is False


def test_bundled_prompts_keep_required_output_contracts(tmp_app_data):
    items = TestClient(create_app()).get("/api/prompts").json()
    active = {p["type"]: p["content"] for p in items if p["is_active"]}

    assert all(token in active["generate"] for token in ("[S#]", "待确认", "只输出 Markdown"))
    assert all(token in active["review"] for token in ('"score"', '"verdict"', '"ready_for_final"'))
    assert "本次需求中的专属" in active["optimize"]
    assert all(token in active["wiki_analyze"] for token in ("page_operations", "source_anchors", "禁止 merge"))
    assert all(token in active["wiki_write"] for token in ('"pages"', "replace_existing", "不决定磁盘路径"))
