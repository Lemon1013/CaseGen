from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import get_engine
from app.main import create_app
from app.models.entities import PromptTemplate
from app.services import prompts_seed
from app.services.prompts_seed import (
    BUNDLED_PROMPT_VERSION,
    normalize_single_active_prompts,
    seed_default_prompts,
)


TEST_POINTS_V7 = (Path(__file__).parent / "fixtures" / "test_points_v7.md").read_text(encoding="utf-8")


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


def test_single_active_for_non_generate_prompt_types(tmp_app_data):
    client = TestClient(create_app())
    first = client.post(
        "/api/prompts",
        json={"name": "review-a", "type": "review", "content": "A", "is_active": True},
    ).json()
    second = client.post(
        "/api/prompts",
        json={"name": "review-b", "type": "review", "content": "B", "is_active": True},
    ).json()

    active = [p for p in client.get("/api/prompts", params={"type": "review"}).json() if p["is_active"]]
    assert [p["id"] for p in active] == [second["id"]]

    client.put(f"/api/prompts/{first['id']}", json={"is_active": True})
    active = [p for p in client.get("/api/prompts", params={"type": "review"}).json() if p["is_active"]]
    assert [p["id"] for p in active] == [first["id"]]


def test_normalize_single_active_prompts_keeps_latest(tmp_app_data):
    TestClient(create_app())
    with get_engine().begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS uq_prompt_templates_single_active"))
    with Session(get_engine()) as session:
        for row in session.exec(
            select(PromptTemplate).where(PromptTemplate.type == "review")
        ).all():
            row.is_active = False
            session.add(row)
        older = PromptTemplate(
            name="review-older",
            type="review",
            content="older",
            version=1,
            is_active=True,
            updated_at=datetime(2020, 1, 1),
        )
        newer = PromptTemplate(
            name="review-newer",
            type="review",
            content="newer",
            version=2,
            is_active=True,
            updated_at=datetime(2021, 1, 1),
        )
        gen_a = PromptTemplate(name="gen-a", type="generate", content="a", version=1, is_active=True)
        gen_b = PromptTemplate(name="gen-b", type="generate", content="b", version=2, is_active=True)
        session.add(older)
        session.add(newer)
        session.add(gen_a)
        session.add(gen_b)
        session.commit()

        normalize_single_active_prompts(session)
        review_active = session.exec(
            select(PromptTemplate).where(
                PromptTemplate.type == "review",
                PromptTemplate.is_active == True,  # noqa: E712
            )
        ).all()
        generate_active = session.exec(
            select(PromptTemplate).where(
                PromptTemplate.type == "generate",
                PromptTemplate.is_active == True,  # noqa: E712
            )
        ).all()

    assert [row.id for row in review_active] == [newer.id]
    assert {gen_a.id, gen_b.id}.issubset({row.id for row in generate_active})


def test_prompt_unique_index_rejects_second_active(tmp_app_data):
    TestClient(create_app())
    with Session(get_engine()) as session:
        for row in session.exec(
            select(PromptTemplate).where(PromptTemplate.type == "review")
        ).all():
            row.is_active = False
            session.add(row)
        session.add(
            PromptTemplate(name="review-a", type="review", content="A", version=1, is_active=True)
        )
        session.add(
            PromptTemplate(name="review-b", type="review", content="B", version=2, is_active=True)
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with Session(get_engine()) as session:
        session.add(
            PromptTemplate(name="gen-a", type="generate", content="A", version=1, is_active=True)
        )
        session.add(
            PromptTemplate(name="gen-b", type="generate", content="B", version=2, is_active=True)
        )
        session.commit()
        active_generate = session.exec(
            select(PromptTemplate).where(
                PromptTemplate.type == "generate",
                PromptTemplate.is_active == True,  # noqa: E712
            )
        ).all()

    assert {"gen-a", "gen-b"}.issubset({row.name for row in active_generate})


def test_create_prompt_returns_409_on_active_conflict(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    for row in client.get("/api/prompts", params={"type": "review"}).json():
        if row["is_active"]:
            assert client.put(
                f"/api/prompts/{row['id']}", json={"is_active": False}
            ).status_code == 200

    monkeypatch.setattr(
        "app.api.prompts._deactivate_active_prompts",
        lambda *args, **kwargs: None,
    )
    first = client.post(
        "/api/prompts",
        json={"name": "review-a", "type": "review", "content": "A", "is_active": True},
    )
    second = client.post(
        "/api/prompts",
        json={"name": "review-b", "type": "review", "content": "B", "is_active": True},
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert "already enabled" in second.json()["detail"]


def test_default_prompts_seeded(tmp_app_data):
    client = TestClient(create_app())
    items = client.get("/api/prompts").json()
    types = {p["type"] for p in items if p["is_active"]}
    assert {
        "generate",
        "test_points",
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


def test_seed_upgrades_v7_test_points_prompt(tmp_app_data):
    TestClient(create_app())
    assert prompts_seed._content_hash(TEST_POINTS_V7) == (
        "109697d4cf72d2390c16b802abcc5c157777910f0594264497e162d4c66f4ba3"
    )

    with Session(get_engine()) as session:
        for row in session.exec(
            select(PromptTemplate).where(PromptTemplate.type == "test_points")
        ).all():
            row.is_active = False
            session.add(row)
        legacy = PromptTemplate(
            name="default_test_points",
            type="test_points",
            content=TEST_POINTS_V7,
            version=7,
            is_active=True,
        )
        session.add(legacy)
        session.commit()
        session.refresh(legacy)

        seed_default_prompts(session)
        rows = session.exec(
            select(PromptTemplate)
            .where(PromptTemplate.type == "test_points")
            .order_by(PromptTemplate.id)
        ).all()

    old = next(row for row in rows if row.id == legacy.id)
    active = [row for row in rows if row.is_active]
    assert old.is_active is False
    assert len(active) == 1
    assert active[0].version >= BUNDLED_PROMPT_VERSION
    assert "model_citation_label" in active[0].content


def test_seed_adds_inactive_bundled_test_points_for_custom_active(tmp_app_data):
    TestClient(create_app())
    with Session(get_engine()) as session:
        for row in session.exec(
            select(PromptTemplate).where(PromptTemplate.type == "test_points")
        ).all():
            row.is_active = False
            session.add(row)
        custom = PromptTemplate(
            name="team-test-points",
            type="test_points",
            content="团队自定义测试点提示词",
            version=8,
            is_active=True,
        )
        session.add(custom)
        session.commit()
        session.refresh(custom)

        seed_default_prompts(session)
        rows = session.exec(
            select(PromptTemplate).where(PromptTemplate.type == "test_points")
        ).all()

    custom = next(row for row in rows if row.id == custom.id)
    bundled = [row for row in rows if row.name == "default_test_points" and "model_citation_label" in row.content]
    assert custom.is_active is True
    assert bundled
    assert all(row.is_active is False for row in bundled)


def test_bundled_prompts_keep_required_output_contracts(tmp_app_data):
    items = TestClient(create_app()).get("/api/prompts").json()
    active = {p["type"]: p["content"] for p in items if p["is_active"]}

    assert all(token in active["generate"] for token in ("[S#]", "待确认", "只输出 Markdown"))
    assert all(token in active["review"] for token in ('"score"', '"verdict"', '"ready_for_final"'))
    assert "本次需求中的专属" in active["optimize"]
    assert all(token in active["wiki_analyze"] for token in ("page_operations", "source_anchors", "禁止 merge"))
    assert all(token in active["wiki_write"] for token in ('"pages"', "replace_existing", "不决定磁盘路径"))
    assert all(
        token in active["test_points"]
        for token in ("model_citation_label", "reference_case_snapshots", "test_point_json_repair_request")
    )


def test_delete_prompt_is_safe_for_runtime_and_history(tmp_app_data):
    client = TestClient(create_app())

    removable = client.post(
        "/api/prompts",
        json={
            "name": "removable-review",
            "type": "review",
            "content": "temporary",
            "is_active": False,
        },
    ).json()
    deleted = client.delete(f"/api/prompts/{removable['id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True, "id": removable["id"]}
    assert client.get(f"/api/prompts/{removable['id']}").status_code == 404

    referenced = client.post(
        "/api/prompts",
        json={
            "name": "task-generate",
            "type": "generate",
            "content": "task prompt",
            "is_active": True,
        },
    ).json()
    task = client.post(
        "/api/tasks",
        json={
            "title": "引用提示词",
            "description": "不能删除其提示词",
            "prompt_template_id": referenced["id"],
        },
    )
    assert task.status_code == 200
    blocked = client.delete(f"/api/prompts/{referenced['id']}")
    assert blocked.status_code == 409
    assert "task" in blocked.json()["detail"].lower()

    items = client.get("/api/prompts", params={"type": "optimize"}).json()
    for item in items:
        if item["is_active"]:
            assert client.put(
                f"/api/prompts/{item['id']}",
                json={"is_active": False},
            ).status_code == 200
    only_active = client.post(
        "/api/prompts",
        json={
            "name": "only-optimize",
            "type": "optimize",
            "content": "only active prompt",
            "is_active": True,
        },
    ).json()
    assert client.delete(f"/api/prompts/{only_active['id']}").status_code == 409
    client.post(
        "/api/prompts",
        json={
            "name": "replacement-optimize",
            "type": "optimize",
            "content": "replacement",
            "is_active": True,
        },
    )
    assert client.delete(f"/api/prompts/{only_active['id']}").status_code == 200
    assert client.delete("/api/prompts/999999").status_code == 404
