"""Comprehensive tests for Phase 2 prompt governance, permissions, fork API,
disabling global prompt overwrite, migration 0003, and single-session middleware.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import (
    _migrate_0003_prompt_visibility,
    get_applied_migrations,
    get_engine,
    init_db,
    reset_engine,
)
from app.main import create_app
from app.models.entities import (
    CaseDraft,
    GenerationTask,
    ModelConfig,
    Project,
    PromptRevision,
    PromptTemplate,
    Requirement,
    ReviewResult,
    TaskEvent,
    TaskTestPointCheckpoint,
    TestPoint as _TestPointRow,
    User,
)
from app.services.auth import hash_password
from app.services.prompts_seed import (
    ADMIN_ONLY_PROMPT_TYPES,
    seed_default_prompts,
)
from app.services.task_pipeline import (
    _resolve_generate_prompt,
    apply_prompt,
    run_optimize_prompt,
)


def _origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


def _setup_auth_env(monkeypatch) -> tuple[TestClient, int]:
    """Enable AUTH_ENABLED, create bootstrap admin, and return (admin_client, default_project_id)."""
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    admin = TestClient(create_app())
    resp = admin.post(
        "/api/auth/setup",
        headers=_origin(),
        json={
            "username": "admin",
            "display_name": "Admin",
            "password": "CaseGen-Test1!",
        },
    )
    assert resp.status_code == 200, resp.text
    with Session(get_engine()) as session:
        project = session.exec(select(Project)).first()
        assert project is not None and project.id is not None
        project_id = int(project.id)
    admin.params = {"project_id": project_id}
    return admin, project_id


def _create_user_and_login(
    username: str,
    project_id: int,
    *,
    role: str = "user",
) -> tuple[TestClient, int]:
    with Session(get_engine()) as session:
        row = User(
            username=username,
            display_name=username.capitalize(),
            password_hash=hash_password("CaseGen-Test1!"),
            role=role,
            is_active=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        user_id = int(row.id)

    client = TestClient(create_app())
    login = client.post(
        "/api/auth/login",
        headers=_origin(),
        json={"username": username, "password": "CaseGen-Test1!"},
    )
    assert login.status_code == 200, login.text
    client.params = {"project_id": project_id}
    return client, user_id


# ---------------------------------------------------------------------------
# 1. Migration 0003_prompt_visibility & seed isolation
# ---------------------------------------------------------------------------


def test_migration_0003_adds_columns_indexes_and_backfills_legacy_prompts(tmp_app_data):
    db_path = tmp_app_data / "meta" / "app.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Simulate a legacy database before 0003 (prompt_templates has no owner_user_id/visibility).
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE prompt_templates (
                id INTEGER PRIMARY KEY,
                name VARCHAR NOT NULL,
                type VARCHAR NOT NULL,
                content VARCHAR NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO prompt_templates (id, name, type, content, version, is_active) "
            "VALUES (1, 'legacy_gen', 'generate', 'legacy content', 2, 1), "
            "(2, 'legacy_review', 'review', 'legacy review', 1, 1)"
        )
        conn.commit()

    reset_engine()
    init_db()

    applied = get_applied_migrations(get_engine())
    assert "0003_prompt_visibility" in applied

    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(prompt_templates)").fetchall()}
        assert "owner_user_id" in cols
        assert "visibility" in cols

        indexes = {row[1] for row in conn.execute("PRAGMA index_list(prompt_templates)").fetchall()}
        assert "ix_prompt_templates_owner_user_id" in indexes
        assert "ix_prompt_templates_visibility" in indexes

        rows = conn.execute(
            "SELECT id, owner_user_id, visibility FROM prompt_templates ORDER BY id"
        ).fetchall()
        assert rows == [(1, None, "system"), (2, None, "system")]

        # Simulate blank/whitespace visibility rows and verify direct re-run idempotency
        # while preserving existing private/shared rows.
        conn.execute(
            "INSERT INTO prompt_templates "
            "(id, name, type, content, version, is_active, owner_user_id, visibility) "
            "VALUES (3, 'blank_vis', 'generate', 'x', 3, 1, NULL, '   '), "
            "(4, 'user_priv', 'generate', 'p', 4, 1, 42, 'private'), "
            "(5, 'user_shared', 'generate', 's', 5, 1, 42, 'shared')"
        )
        conn.commit()

    _migrate_0003_prompt_visibility(get_engine())
    # Repeated init_db must also stay idempotent.
    init_db()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, owner_user_id, visibility FROM prompt_templates ORDER BY id"
        ).fetchall()
        assert rows == [
            (1, None, "system"),
            (2, None, "system"),
            (3, None, "system"),
            (4, 42, "private"),
            (5, 42, "shared"),
        ]


def test_seed_default_prompts_only_targets_system_visibility(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        # Insert user private & shared generate prompts before seeding.
        priv = PromptTemplate(
            name="alice_private",
            type="generate",
            content="alice private content",
            version=1,
            is_active=True,
            owner_user_id=101,
            visibility="private",
        )
        shared = PromptTemplate(
            name="alice_shared",
            type="generate",
            content="alice shared content",
            version=2,
            is_active=True,
            owner_user_id=101,
            visibility="shared",
        )
        session.add(priv)
        session.add(shared)
        session.commit()
        session.refresh(priv)
        session.refresh(shared)

        # Because no system generate prompt exists yet, seed_default_prompts must
        # still seed default_generate as visibility='system', owner_user_id=None
        # without disabling or altering the user's private/shared templates.
        seed_default_prompts(session)

        priv_after = session.get(PromptTemplate, priv.id)
        shared_after = session.get(PromptTemplate, shared.id)
        sys_gen = session.exec(
            select(PromptTemplate).where(
                PromptTemplate.type == "generate",
                PromptTemplate.visibility == "system",
                PromptTemplate.is_active == True,  # noqa: E712
            )
        ).all()

    assert priv_after is not None and priv_after.is_active is True and priv_after.visibility == "private"
    assert shared_after is not None and shared_after.is_active is True and shared_after.visibility == "shared"
    assert len(sys_gen) == 1
    assert sys_gen[0].name == "default_generate"
    assert sys_gen[0].owner_user_id is None
    assert sys_gen[0].visibility == "system"


# ---------------------------------------------------------------------------
# 2. Ordinary users cannot see, inspect, edit, delete, or create engine prompts
# ---------------------------------------------------------------------------


def test_ordinary_user_cannot_access_or_create_engine_prompts(tmp_app_data, monkeypatch):
    admin, project_id = _setup_auth_env(monkeypatch)
    alice, _ = _create_user_and_login("alice", project_id)

    admin_list = admin.get("/api/prompts", headers=_origin()).json()
    engine_prompts = [p for p in admin_list if p["type"] in ADMIN_ONLY_PROMPT_TYPES]
    assert {p["type"] for p in engine_prompts} == set(ADMIN_ONLY_PROMPT_TYPES)
    # Admin sees can_edit=True, can_delete=True, can_fork=False on engine prompts.
    for ep in engine_prompts:
        assert ep["visibility"] == "system"
        assert ep["owner_user_id"] is None
        assert ep["can_edit"] is True
        assert ep["can_delete"] is True
        assert ep["can_fork"] is False

    # Ordinary user list contains ONLY generate prompts, never any engine prompt.
    alice_list = alice.get("/api/prompts", headers=_origin())
    assert alice_list.status_code == 200
    assert alice_list.json()
    assert all(p["type"] == "generate" for p in alice_list.json())

    for engine_type in ADMIN_ONLY_PROMPT_TYPES:
        # Filtered list returns empty
        by_type = alice.get("/api/prompts", params={"type": engine_type}, headers=_origin())
        assert by_type.status_code == 200
        assert by_type.json() == []

        # Create returns 403
        create_resp = alice.post(
            "/api/prompts",
            headers=_origin(),
            json={"name": f"try-{engine_type}", "type": engine_type, "content": "hack"},
        )
        assert create_resp.status_code == 403, f"Expected 403 for {engine_type}, got {create_resp.status_code}"

    # Single-item GET / PUT / DELETE / fork on every engine prompt returns 404 for ordinary user.
    for ep in engine_prompts:
        pid = ep["id"]
        assert alice.get(f"/api/prompts/{pid}", headers=_origin()).status_code == 404
        assert alice.put(f"/api/prompts/{pid}", headers=_origin(), json={"content": "hack"}).status_code == 404
        assert alice.delete(f"/api/prompts/{pid}", headers=_origin()).status_code == 404
        assert alice.post(f"/api/prompts/{pid}/fork", headers=_origin(), json={}).status_code == 404

    # Invalid prompt type returns 422.
    invalid_type = admin.post(
        "/api/prompts",
        headers=_origin(),
        json={"name": "bad", "type": "unknown_type", "content": "x"},
    )
    assert invalid_type.status_code == 422

    # Admin cannot change engine prompt visibility away from system (422) or fork engine prompt (422).
    review_id = next(ep["id"] for ep in engine_prompts if ep["type"] == "review")
    assert admin.put(
        f"/api/prompts/{review_id}",
        headers=_origin(),
        json={"visibility": "private"},
    ).status_code == 422
    assert admin.post(
        f"/api/prompts/{review_id}/fork",
        headers=_origin(),
        json={},
    ).status_code == 422


# ---------------------------------------------------------------------------
# 3. Ordinary user creating private/shared vs forbidden system
# ---------------------------------------------------------------------------


def test_ordinary_user_prompt_creation_and_visibility_rules(tmp_app_data, monkeypatch):
    admin, project_id = _setup_auth_env(monkeypatch)
    alice, alice_id = _create_user_and_login("alice", project_id)

    # Default visibility for ordinary user is private.
    r_default = alice.post(
        "/api/prompts",
        headers=_origin(),
        json={"name": "alice-default", "type": "generate", "content": "my private prompt"},
    )
    assert r_default.status_code == 200, r_default.text
    p_priv = r_default.json()
    assert p_priv["visibility"] == "private"
    assert p_priv["owner_user_id"] == alice_id
    assert p_priv["owner_username"] == "alice"
    assert p_priv["can_edit"] is True
    assert p_priv["can_delete"] is True
    assert p_priv["can_fork"] is True

    # Explicit shared creation succeeds.
    r_shared = alice.post(
        "/api/prompts",
        headers=_origin(),
        json={
            "name": "alice-shared",
            "type": "generate",
            "content": "my shared prompt",
            "visibility": "shared",
        },
    )
    assert r_shared.status_code == 200, r_shared.text
    p_shared = r_shared.json()
    assert p_shared["visibility"] == "shared"
    assert p_shared["owner_user_id"] == alice_id
    assert p_shared["owner_username"] == "alice"

    # Attempting to create a system prompt is 403.
    r_sys = alice.post(
        "/api/prompts",
        headers=_origin(),
        json={
            "name": "alice-sys",
            "type": "generate",
            "content": "nope",
            "visibility": "system",
        },
    )
    assert r_sys.status_code == 403

    # Attempting to upgrade own prompt to system is 403.
    assert alice.put(
        f"/api/prompts/{p_priv['id']}",
        headers=_origin(),
        json={"visibility": "system"},
    ).status_code == 403

    # Alice can toggle own prompt between private and shared, and delete own prompt freely.
    toggled = alice.put(
        f"/api/prompts/{p_priv['id']}",
        headers=_origin(),
        json={"visibility": "shared"},
    )
    assert toggled.status_code == 200
    assert toggled.json()["visibility"] == "shared"
    assert toggled.json()["owner_user_id"] == alice_id

    deleted = alice.delete(f"/api/prompts/{p_priv['id']}", headers=_origin())
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True, "id": p_priv["id"]}

    # Deleting the last active system generate prompt is still protected (409) even when shared exists.
    sys_gens = [
        p
        for p in admin.get("/api/prompts", params={"type": "generate", "visibility": "system"}, headers=_origin()).json()
        if p["is_active"]
    ]
    assert len(sys_gens) == 1
    assert admin.delete(f"/api/prompts/{sys_gens[0]['id']}", headers=_origin()).status_code == 409


# ---------------------------------------------------------------------------
# 4. Strict privacy red line: private prompt invisible to other users AND admin
# ---------------------------------------------------------------------------


def test_private_prompt_strict_isolation_including_admin(tmp_app_data, monkeypatch):
    admin, project_id = _setup_auth_env(monkeypatch)
    alice, alice_id = _create_user_and_login("alice", project_id)
    bob, _ = _create_user_and_login("bob", project_id)

    created = alice.post(
        "/api/prompts",
        headers=_origin(),
        json={
            "name": "alice-secret-template",
            "type": "generate",
            "content": "TOP SECRET PROMPT",
            "visibility": "private",
        },
    )
    assert created.status_code == 200
    secret_id = created.json()["id"]
    assert created.json()["owner_user_id"] == alice_id

    # Alice sees it in list and can use it in task creation.
    alice_ids = {p["id"] for p in alice.get("/api/prompts", headers=_origin()).json()}
    assert secret_id in alice_ids
    alice_task = alice.post(
        "/api/tasks",
        headers=_origin(),
        json={
            "title": "Alice 使用自己的私有模板",
            "description": "正常允许",
            "prompt_template_id": secret_id,
        },
    )
    assert alice_task.status_code == 200, alice_task.text
    assert alice_task.json()["prompt_template_id"] == secret_id

    # Neither ordinary user Bob NOR administrator can see, inspect, mutate, fork, or bind it to a task!
    for actor_name, actor_client in (("bob", bob), ("admin", admin)):
        all_items = actor_client.get("/api/prompts", headers=_origin()).json()
        priv_items = actor_client.get("/api/prompts", params={"visibility": "private"}, headers=_origin()).json()
        assert secret_id not in {p["id"] for p in all_items}, f"{actor_name} leaked private prompt in list"
        assert secret_id not in {p["id"] for p in priv_items}, f"{actor_name} leaked private prompt in private filter"

        r_get = actor_client.get(f"/api/prompts/{secret_id}", headers=_origin())
        assert r_get.status_code == 404, f"{actor_name} GET expected 404, got {r_get.status_code}"
        assert r_get.json()["detail"] == "Prompt not found"

        r_put = actor_client.put(
            f"/api/prompts/{secret_id}",
            headers=_origin(),
            json={"content": "tampered"},
        )
        assert r_put.status_code == 404, f"{actor_name} PUT expected 404, got {r_put.status_code}"

        r_del = actor_client.delete(f"/api/prompts/{secret_id}", headers=_origin())
        assert r_del.status_code == 404, f"{actor_name} DELETE expected 404, got {r_del.status_code}"

        r_fork = actor_client.post(f"/api/prompts/{secret_id}/fork", headers=_origin(), json={})
        assert r_fork.status_code == 404, f"{actor_name} FORK expected 404, got {r_fork.status_code}"

        r_task = actor_client.post(
            "/api/tasks",
            headers=_origin(),
            json={
                "title": f"{actor_name} 越权任务",
                "description": "不应允许引用他人私有模板",
                "prompt_template_id": secret_id,
            },
        )
        assert r_task.status_code == 404, f"{actor_name} create_task expected 404, got {r_task.status_code}"
        assert r_task.json()["detail"] == "Prompt not found"


def test_default_generate_prompt_resolution_never_picks_private(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        seed_default_prompts(session)
        req = Requirement(title="req", description="desc")
        session.add(req)
        session.flush()
        task = GenerationTask(requirement_id=int(req.id), status="draft")
        session.add(task)
        session.flush()

        # Add a newer private prompt and a newer shared prompt.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        future_t1 = now + timedelta(hours=1)
        future_t2 = now + timedelta(hours=2)
        shared = PromptTemplate(
            name="team-shared",
            type="generate",
            content="SHARED_CONTENT",
            version=20,
            is_active=True,
            owner_user_id=10,
            visibility="shared",
            updated_at=future_t1,
        )
        priv = PromptTemplate(
            name="user-priv",
            type="generate",
            content="PRIVATE_CONTENT",
            version=21,
            is_active=True,
            owner_user_id=11,
            visibility="private",
            updated_at=future_t2,
        )
        session.add(shared)
        session.add(priv)
        session.commit()
        session.refresh(shared)
        session.refresh(priv)

        # 1. System active exists -> must pick system even though shared/private are newer.
        content, _ = _resolve_generate_prompt(session, task)
        assert content != "PRIVATE_CONTENT"
        assert content != "SHARED_CONTENT"

        # Also verify run_optimize_prompt picks system (not private) as base_prompt_id.
        model = ModelConfig(
            name="opt-m",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model_name="gpt-test",
            is_default=True,
        )
        session.add(model)
        task.status = "reviewed"
        session.add(task)
        session.flush()
        draft = CaseDraft(task_id=int(task.id), version=1, content_md="# TC-001")
        session.add(draft)
        session.flush()
        review = ReviewResult(
            task_id=int(task.id),
            draft_id=int(draft.id),
            score=75,
            verdict="revise",
            payload_json='{"issues":["x"]}',
        )
        session.add(review)
        session.commit()

        run_optimize_prompt(session, int(task.id), chat_fn=lambda **kwargs: "optimized body")
        rev = session.exec(
            select(PromptRevision).where(PromptRevision.task_id == int(task.id))
        ).first()
        assert rev is not None
        assert rev.base_prompt_id is not None
        assert rev.base_prompt_id != priv.id
        assert rev.base_prompt_id != shared.id

        # 2. Deactivate system generate prompts -> falls back to shared, NEVER private.
        for row in session.exec(
            select(PromptTemplate).where(
                PromptTemplate.type == "generate",
                PromptTemplate.visibility == "system",
            )
        ).all():
            row.is_active = False
            session.add(row)
        session.commit()

        content2, _ = _resolve_generate_prompt(session, task)
        assert content2 == "SHARED_CONTENT"

        # 3. Deactivate shared too -> only private remains -> must raise RuntimeError, never leak private!
        shared.is_active = False
        session.add(shared)
        session.commit()

        with pytest.raises(RuntimeError, match="No active generate prompt template found"):
            _resolve_generate_prompt(session, task)


# ---------------------------------------------------------------------------
# 5. Shared template collaboration, Fork API & Admin governance
# ---------------------------------------------------------------------------


def test_shared_prompt_collaboration_fork_and_admin_governance(tmp_app_data, monkeypatch):
    admin, project_id = _setup_auth_env(monkeypatch)
    alice, alice_id = _create_user_and_login("alice", project_id)
    bob, bob_id = _create_user_and_login("bob", project_id)

    # Alice creates a shared prompt.
    created = alice.post(
        "/api/prompts",
        headers=_origin(),
        json={
            "name": "团队清算模板",
            "type": "generate",
            "content": "共享提示词正文 v1",
            "visibility": "shared",
        },
    )
    assert created.status_code == 200
    shared_id = created.json()["id"]

    # Bob sees it with correct permissions & owner_username.
    bob_get = bob.get(f"/api/prompts/{shared_id}", headers=_origin())
    assert bob_get.status_code == 200
    bob_view = bob_get.json()
    assert bob_view["owner_user_id"] == alice_id
    assert bob_view["owner_username"] == "alice"
    assert bob_view["visibility"] == "shared"
    assert bob_view["can_edit"] is False
    assert bob_view["can_delete"] is False
    assert bob_view["can_fork"] is True

    # Bob can use it in a task.
    bob_task = bob.post(
        "/api/tasks",
        headers=_origin(),
        json={
            "title": "Bob 引用共享模板",
            "description": "测试任务",
            "prompt_template_id": shared_id,
        },
    )
    assert bob_task.status_code == 200

    # Bob can fork it into his own private prompt (default name & default visibility='private').
    bob_fork = bob.post(f"/api/prompts/{shared_id}/fork", headers=_origin(), json={})
    assert bob_fork.status_code == 200, bob_fork.text
    forked = bob_fork.json()
    assert forked["id"] != shared_id
    assert forked["type"] == "generate"
    assert forked["name"] == "团队清算模板 (副本)"
    assert forked["content"] == "共享提示词正文 v1"
    assert forked["visibility"] == "private"
    assert forked["owner_user_id"] == bob_id
    assert forked["owner_username"] == "bob"
    assert forked["is_active"] is True
    assert forked["can_edit"] is True
    assert forked["can_delete"] is True
    assert forked["can_fork"] is True

    # Bob cannot edit or delete Alice's shared prompt (403).
    assert bob.put(f"/api/prompts/{shared_id}", headers=_origin(), json={"content": "bob edit"}).status_code == 403
    assert bob.delete(f"/api/prompts/{shared_id}", headers=_origin()).status_code == 403

    # Admin sees Alice's shared prompt with governance permissions.
    admin_view = admin.get(f"/api/prompts/{shared_id}", headers=_origin()).json()
    assert admin_view["can_edit"] is True
    assert admin_view["can_delete"] is True
    assert admin_view["can_fork"] is True
    assert admin_view["owner_username"] == "alice"

    # Admin can govern (edit content / disable) while keeping Alice as owner.
    admin_edit = admin.put(
        f"/api/prompts/{shared_id}",
        headers=_origin(),
        json={"content": "管理员合规修订正文", "is_active": False},
    )
    assert admin_edit.status_code == 200
    assert admin_edit.json()["content"] == "管理员合规修订正文"
    assert admin_edit.json()["is_active"] is False
    assert admin_edit.json()["owner_user_id"] == alice_id
    assert admin_edit.json()["visibility"] == "shared"

    # Admin MUST NOT convert another user's shared prompt to private (403) or system (403).
    assert admin.put(
        f"/api/prompts/{shared_id}",
        headers=_origin(),
        json={"visibility": "private"},
    ).status_code == 403
    assert admin.put(
        f"/api/prompts/{shared_id}",
        headers=_origin(),
        json={"visibility": "system"},
    ).status_code == 403

    # Admin can promote their OWN shared/private prompt to system (clearing owner_user_id).
    admin_own = admin.post(
        "/api/prompts",
        headers=_origin(),
        json={
            "name": "admin-draft",
            "type": "generate",
            "content": "admin own",
            "visibility": "private",
        },
    ).json()
    assert admin_own["owner_user_id"] is not None
    promoted = admin.put(
        f"/api/prompts/{admin_own['id']}",
        headers=_origin(),
        json={"visibility": "system"},
    )
    assert promoted.status_code == 200
    assert promoted.json()["visibility"] == "system"
    assert promoted.json()["owner_user_id"] is None
    assert promoted.json()["owner_username"] is None

    # Create a second unreferenced shared prompt by Alice and verify Admin can delete it (200).
    unreferenced_shared = alice.post(
        "/api/prompts",
        headers=_origin(),
        json={
            "name": "待清理共享模板",
            "type": "generate",
            "content": "to delete",
            "visibility": "shared",
        },
    ).json()
    assert admin.delete(f"/api/prompts/{unreferenced_shared['id']}", headers=_origin()).status_code == 200


# ---------------------------------------------------------------------------
# 6. System preset template: ordinary user can view/use/fork, cannot edit/delete
# ---------------------------------------------------------------------------


def test_system_generate_prompt_permissions_for_ordinary_user(tmp_app_data, monkeypatch):
    _, project_id = _setup_auth_env(monkeypatch)
    alice, alice_id = _create_user_and_login("alice", project_id)

    items = alice.get("/api/prompts", params={"type": "generate", "visibility": "system"}, headers=_origin()).json()
    assert len(items) >= 1
    sys_prompt = next(p for p in items if p["name"] == "default_generate")
    assert sys_prompt["visibility"] == "system"
    assert sys_prompt["owner_user_id"] is None
    assert sys_prompt["owner_username"] is None
    assert sys_prompt["can_edit"] is False
    assert sys_prompt["can_delete"] is False
    assert sys_prompt["can_fork"] is True

    # Usable in task creation.
    task_resp = alice.post(
        "/api/tasks",
        headers=_origin(),
        json={
            "title": "使用系统模板",
            "description": "描述",
            "prompt_template_id": sys_prompt["id"],
        },
    )
    assert task_resp.status_code == 200

    # Forkable with custom name & shared visibility.
    forked_resp = alice.post(
        f"/api/prompts/{sys_prompt['id']}/fork",
        headers=_origin(),
        json={"name": "我的定制版", "visibility": "shared"},
    )
    assert forked_resp.status_code == 200
    forked = forked_resp.json()
    assert forked["name"] == "我的定制版"
    assert forked["visibility"] == "shared"
    assert forked["owner_user_id"] == alice_id
    assert forked["content"] == sys_prompt["content"]

    # Cannot edit or delete system prompt (403).
    assert alice.put(
        f"/api/prompts/{sys_prompt['id']}",
        headers=_origin(),
        json={"content": "modify system"},
    ).status_code == 403
    assert alice.delete(f"/api/prompts/{sys_prompt['id']}", headers=_origin()).status_code == 403


# ---------------------------------------------------------------------------
# 7. Disable apply_prompt(mode="global"), keep task_temp working
# ---------------------------------------------------------------------------


def test_apply_prompt_rejects_global_mode_and_allows_task_temp(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        seed_default_prompts(session)
        req = Requirement(title="需求", description="说明")
        session.add(req)
        session.flush()
        task = GenerationTask(requirement_id=int(req.id), status="reviewed")
        session.add(task)
        session.flush()
        rev = PromptRevision(
            task_id=int(task.id),
            new_content="优化后的提示词正文",
            status="pending",
        )
        session.add(rev)
        session.commit()
        task_id = int(task.id)
        rev_id = int(rev.id)
        before_prompt_count = len(session.exec(select(PromptTemplate)).all())

    client = TestClient(create_app())

    # HTTP mode="global" must return 422 with explicit Chinese error message.
    resp_global = client.post(
        f"/api/tasks/{task_id}/apply-prompt",
        json={"revision_id": rev_id, "mode": "global"},
    )
    assert resp_global.status_code == 422
    assert resp_global.json()["detail"] == "已禁止修改全局提示词，仅支持应用到当前任务（task_temp）"

    # Any non-task_temp mode must also return 422 with the same clear message.
    resp_other = client.post(
        f"/api/tasks/{task_id}/apply-prompt",
        json={"revision_id": rev_id, "mode": "invalid_mode"},
    )
    assert resp_other.status_code == 422
    assert "已禁止修改全局提示词" in resp_other.json()["detail"]

    # Service-layer direct call also rejects mode="global".
    with Session(get_engine()) as session:
        with pytest.raises(ValueError, match="已禁止修改全局提示词"):
            apply_prompt(session, task_id, rev_id, mode="global")
        after_prompt_count = len(session.exec(select(PromptTemplate)).all())
        assert after_prompt_count == before_prompt_count
        rev_row = session.get(PromptRevision, rev_id)
        assert rev_row is not None and rev_row.status == "pending"

    # mode="task_temp" continues to work normally.
    resp_temp = client.post(
        f"/api/tasks/{task_id}/apply-prompt",
        json={"revision_id": rev_id, "mode": "task_temp"},
    )
    assert resp_temp.status_code == 200
    with Session(get_engine()) as session:
        task_row = session.get(GenerationTask, task_id)
        rev_row = session.get(PromptRevision, rev_id)
        assert task_row is not None and task_row.temp_prompt_content == "优化后的提示词正文"
        assert rev_row is not None and rev_row.status == "applied_task_temp"
        assert len(session.exec(select(PromptTemplate)).all()) == before_prompt_count


# ---------------------------------------------------------------------------
# 8. Single-session authentication middleware & closing before call_next
# ---------------------------------------------------------------------------


def test_authentication_middleware_uses_single_session_closed_before_route(tmp_app_data, monkeypatch):
    import app.main as main_module

    admin, project_id = _setup_auth_env(monkeypatch)
    alice, _ = _create_user_and_login("alice", project_id)

    # Create a task in the default project so /api/tasks/{id} exercises auth + visibility + scope.
    created_task = alice.post(
        "/api/tasks",
        headers=_origin(),
        json={"title": "中间件单会话测试", "description": "desc"},
    )
    assert created_task.status_code == 200
    task_id = created_task.json()["id"]

    # Create a second project so we also verify cross-project scope isolation stays intact.
    proj2 = admin.post(
        "/api/projects",
        headers=_origin(),
        json={"name": "OtherProj", "slug": "other-proj"},
    ).json()

    real_session_cls = main_module.Session
    opened_sessions: list[dict[str, bool]] = []

    class TrackingSession(real_session_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._tracker = {"closed": False}
            opened_sessions.append(self._tracker)

        def __exit__(self, exc_type, exc, tb):
            try:
                return super().__exit__(exc_type, exc, tb)
            finally:
                self._tracker["closed"] = True

    monkeypatch.setattr(main_module, "Session", TrackingSession)

    # Observe inside the real /api/tasks/{task_id} handler to confirm middleware's
    # session is already closed before call_next invokes the route handler.
    import app.api.tasks as tasks_api

    route_observed_closed: list[bool] = []
    real_to_task_out = tasks_api.to_task_out

    def _wrapped_to_task_out(session, task, *args, **kwargs):
        route_observed_closed.append(all(item["closed"] for item in opened_sessions))
        return real_to_task_out(session, task, *args, **kwargs)

    monkeypatch.setattr(tasks_api, "to_task_out", _wrapped_to_task_out)

    # Create a hidden project to verify visibility check also runs inside the single session.
    hidden_proj = admin.post(
        "/api/projects",
        headers=_origin(),
        json={"name": "HiddenProj", "slug": "hidden-proj"},
    ).json()
    assert admin.put(
        f"/api/projects/{hidden_proj['id']}",
        headers=_origin(),
        params={"project_id": hidden_proj["id"]},
        json={"is_public": False},
    ).status_code == 200

    app = create_app()
    opened_sessions.clear()  # discard startup seed session

    probe_client = TestClient(app)
    assert probe_client.post(
        "/api/auth/login",
        headers=_origin(),
        json={"username": "alice", "password": "CaseGen-Test1!"},
    ).status_code == 200
    opened_sessions.clear()

    # Single GET /api/tasks/{task_id}?project_id=... exercises auth + visibility + project scope
    # and must open AT MOST 1 middleware Session, closed BEFORE route execution!
    r_task = probe_client.get(
        f"/api/tasks/{task_id}",
        params={"project_id": project_id},
        headers=_origin(),
    )
    assert r_task.status_code == 200
    assert len(opened_sessions) == 1
    assert opened_sessions[0]["closed"] is True
    assert route_observed_closed == [True]

    # Hidden project check returns 404 inside the same single session.
    opened_sessions.clear()
    r_hidden = probe_client.get(
        "/api/tasks",
        params={"project_id": hidden_proj["id"]},
        headers=_origin(),
    )
    assert r_hidden.status_code == 404
    assert len(opened_sessions) == 1
    assert opened_sessions[0]["closed"] is True

    # Cross-project access to task_id via proj2 is still rejected with 404 (single session).
    opened_sessions.clear()
    r_cross = probe_client.get(
        f"/api/tasks/{task_id}",
        params={"project_id": proj2["id"]},
        headers=_origin(),
    )
    assert r_cross.status_code == 404
    assert len(opened_sessions) == 1
    assert opened_sessions[0]["closed"] is True

    # Missing Origin on unsafe request still returns 403 and closes middleware session.
    opened_sessions.clear()
    r_csrf = probe_client.post(
        "/api/tasks",
        params={"project_id": project_id},
        json={"title": "no-origin", "description": "d"},
    )
    assert r_csrf.status_code == 403
    assert len(opened_sessions) == 1
    assert opened_sessions[0]["closed"] is True

    # Unauthenticated request returns 401 and closes middleware session.
    anon = TestClient(app)
    opened_sessions.clear()
    r_unauth = anon.get("/api/tasks", params={"project_id": project_id})
    assert r_unauth.status_code == 401
    assert len(opened_sessions) == 1
    assert opened_sessions[0]["closed"] is True

    # Stateless x-csrf-token mismatch is rejected with 403 BEFORE opening any DB session.
    opened_sessions.clear()
    r_bad_csrf = probe_client.post(
        "/api/tasks",
        params={"project_id": project_id},
        headers={**_origin(), "X-CSRF-Token": "wrong-token"},
        json={"title": "bad-csrf", "description": "d"},
    )
    assert r_bad_csrf.status_code == 403
    assert r_bad_csrf.json()["detail"] == "CSRF validation failed"
    assert len(opened_sessions) == 0

    # Unicode superscript digit "²" and "0" project_id must return 422 (never 500 or bypass).
    for bad_pid in ("²", "0", "-1", "abc"):
        r_bad_pid = probe_client.get(
            "/api/tasks",
            params={"project_id": bad_pid},
            headers=_origin(),
        )
        assert r_bad_pid.status_code == 422, f"Expected 422 for project_id={bad_pid!r}, got {r_bad_pid.status_code}"
        assert r_bad_pid.json()["detail"] == "project_id must be a positive integer"


# ---------------------------------------------------------------------------
# 9. Cross-task private prompt side-channel & downgrade defense (Paths A & B)
# ---------------------------------------------------------------------------


def test_cross_task_private_prompt_side_channel_blocked_path_a(tmp_app_data, monkeypatch):
    """Path A: Alice creates a task bound to her private prompt in a shared project.
    Neither admin nor Bob may leak its content, id, or drive prompt-reading actions."""
    admin, project_id = _setup_auth_env(monkeypatch)
    alice, _ = _create_user_and_login("alice", project_id)
    bob, _ = _create_user_and_login("bob", project_id)

    secret_body = "ALICE_PRIVATE_SECRET_CONTENT_999"
    p_resp = alice.post(
        "/api/prompts",
        headers=_origin(),
        json={
            "name": "alice-private-task-prompt",
            "type": "generate",
            "content": secret_body,
            "visibility": "private",
        },
    )
    assert p_resp.status_code == 200
    secret_id = p_resp.json()["id"]

    # Seed a default model so optimize/generate can run.
    with Session(get_engine()) as session:
        session.add(
            ModelConfig(
                name="m1",
                base_url="https://api.example.com/v1",
                api_key="sk-test",
                model_name="gpt-test",
                is_default=True,
            )
        )
        session.commit()

    task_resp = alice.post(
        "/api/tasks",
        headers=_origin(),
        json={
            "title": "Alice 私有模板任务",
            "description": "在公开项目中创建的任务",
            "prompt_template_id": secret_id,
        },
    )
    assert task_resp.status_code == 200
    task_id = task_resp.json()["id"]
    assert task_resp.json()["prompt_template_id"] == secret_id

    # Seed draft, review, test-point checkpoint, and events referencing secret_id.
    with Session(get_engine()) as session:
        task_row = session.get(GenerationTask, task_id)
        assert task_row is not None
        task_row.status = "reviewed"
        session.add(task_row)
        draft = CaseDraft(
            task_id=task_id,
            version=1,
            content_md="## TC-001\n- 优先级：P1",
            prompt_version_ref=f"id:{secret_id}:v1",
        )
        session.add(draft)
        session.flush()
        review = ReviewResult(
            task_id=task_id,
            draft_id=int(draft.id),
            score=78,
            verdict="revise",
            payload_json='{"issues":["细化步骤"]}',
        )
        session.add(review)
        tp_cp = TaskTestPointCheckpoint(
            task_id=task_id,
            attempt=1,
            version=1,
            status="pending",
        )
        session.add(tp_cp)
        session.flush()
        tp = _TestPointRow(
            task_id=task_id,
            checkpoint_id=int(tp_cp.id),
            stable_key="TP-001",
            title="验证点1",
            verification_goal="目标",
            dimension="positive",
            priority="P1",
            sort_order=0,
        )
        session.add(tp)
        session.add(
            TaskEvent(
                task_id=task_id,
                step="generate",
                message="已生成草稿",
                detail_json=json.dumps({"prompt_ref": f"id:{secret_id}:v1", "draft_version": 1}),
            )
        )
        session.commit()
        tp_cp_version = int(tp_cp.version)

    # Alice optimizes the prompt synchronously so a real PromptRevision exists.
    chat_calls: list[str] = []

    def _fake_opt_chat(**kwargs):
        joined = "\n".join(str(m.get("content", "")) for m in (kwargs.get("messages") or []))
        chat_calls.append(joined)
        return f"OPTIMIZED_BASED_ON_{secret_body}"

    monkeypatch.setattr("app.api.tasks._OPTIMIZE_CHAT_FN", _fake_opt_chat)
    monkeypatch.setattr("app.services.task_pipeline._OPTIMIZE_CHAT_FN", _fake_opt_chat)
    monkeypatch.setattr("app.api.tasks._GENERATE_CHAT_FN", _fake_opt_chat)
    monkeypatch.setattr("app.services.task_pipeline._GENERATE_CHAT_FN", _fake_opt_chat)

    alice_opt = alice.post(f"/api/tasks/{task_id}/optimize-prompt", headers=_origin())
    assert alice_opt.status_code == 200
    assert len(chat_calls) == 1
    assert secret_body in chat_calls[0]

    # Alice sees her own task prompt_template_id, revision, draft ref, and event detail.
    alice_revs = alice.get(f"/api/tasks/{task_id}/revisions", headers=_origin()).json()
    assert len(alice_revs) == 1
    rev_id = alice_revs[0]["id"]
    assert alice_revs[0]["base_prompt_id"] == secret_id
    assert secret_body in alice_revs[0]["new_content"]
    assert alice.get(f"/api/tasks/{task_id}", headers=_origin()).json()["prompt_template_id"] == secret_id
    assert alice.get(f"/api/tasks/{task_id}/drafts", headers=_origin()).json()[0]["prompt_version_ref"] == f"id:{secret_id}:v1"

    # Both admin AND bob must be completely blocked from reading or driving Alice's private prompt!
    chat_calls.clear()
    for actor_name, actor_client in (("admin", admin), ("bob", bob)):
        # List & detail mask prompt_template_id to None
        listed = actor_client.get("/api/tasks", headers=_origin()).json()
        task_in_list = next(item for item in listed if item["id"] == task_id)
        assert task_in_list["prompt_template_id"] is None, f"{actor_name} saw private prompt_template_id in list"

        detail = actor_client.get(f"/api/tasks/{task_id}", headers=_origin()).json()
        assert detail["prompt_template_id"] is None, f"{actor_name} saw private prompt_template_id in detail"

        # Revisions returns [] — zero leak of new_content or base_prompt_id
        revs = actor_client.get(f"/api/tasks/{task_id}/revisions", headers=_origin())
        assert revs.status_code == 200
        assert revs.json() == [], f"{actor_name} leaked revisions: {revs.json()}"

        # Drafts masks prompt_version_ref to "private"
        drafts = actor_client.get(f"/api/tasks/{task_id}/drafts", headers=_origin()).json()
        assert len(drafts) == 1
        assert drafts[0]["prompt_version_ref"] == "private"

        # Events masks prompt_ref & strips base_prompt_id
        events = actor_client.get(f"/api/tasks/{task_id}/events", headers=_origin()).json()
        for ev in events:
            if not ev["detail_json"]:
                continue
            parsed = json.loads(ev["detail_json"])
            assert "base_prompt_id" not in parsed, f"{actor_name} leaked base_prompt_id in events"
            assert "prompt_template_id" not in parsed
            if "prompt_ref" in parsed:
                assert parsed["prompt_ref"] == "private"
            assert str(secret_id) not in ev["detail_json"]

        # Action endpoints MUST all return 403 and NEVER invoke LLM!
        for endpoint, body_json in (
            (f"/api/tasks/{task_id}/optimize-prompt", None),
            (f"/api/tasks/{task_id}/generate", None),
            (f"/api/tasks/{task_id}/regenerate", None),
            (f"/api/tasks/{task_id}/apply-prompt", {"revision_id": rev_id, "mode": "task_temp"}),
            (
                f"/api/tasks/{task_id}/test-points/confirm",
                {
                    "points": [
                        {
                            "stable_key": "TP-001",
                            "title": "验证点1",
                            "verification_goal": "目标",
                            "dimension": "positive",
                            "priority": "P1",
                            "sort_order": 0,
                            "is_selected": True,
                            "is_excluded": False,
                            "citation_ids": [],
                        }
                    ],
                    "expected_version": tp_cp_version,
                    "idempotency_key": f"blocked-{actor_name}",
                },
            ),
        ):
            r = (
                actor_client.post(endpoint, headers=_origin(), json=body_json)
                if body_json is not None
                else actor_client.post(endpoint, headers=_origin())
            )
            assert r.status_code == 403, f"{actor_name} {endpoint} expected 403, got {r.status_code}: {r.text}"
            assert "私有提示词模板" in r.json()["detail"]
            assert secret_body not in r.text

    assert chat_calls == []


def test_shared_to_private_downgrade_blocks_previously_bound_task_path_b(tmp_app_data, monkeypatch):
    """Path B: Alice creates shared prompt -> Admin binds it to a task -> Alice downgrades
    prompt to private and writes secret content -> Admin is immediately blocked from
    optimizing, regenerating, applying, or reading revisions of that task."""
    admin, project_id = _setup_auth_env(monkeypatch)
    alice, _ = _create_user_and_login("alice", project_id)

    created = alice.post(
        "/api/prompts",
        headers=_origin(),
        json={
            "name": "originally-shared",
            "type": "generate",
            "content": "SHARED_V1_BODY",
            "visibility": "shared",
        },
    )
    assert created.status_code == 200
    prompt_id = created.json()["id"]

    with Session(get_engine()) as session:
        session.add(
            ModelConfig(
                name="m2",
                base_url="https://api.example.com/v1",
                api_key="sk-test",
                model_name="gpt-test",
                is_default=True,
            )
        )
        session.commit()

    # Admin legitimately binds the shared prompt to a task.
    task_resp = admin.post(
        "/api/tasks",
        headers=_origin(),
        json={
            "title": "Admin 绑定共享模板的任务",
            "description": "desc",
            "prompt_template_id": prompt_id,
        },
    )
    assert task_resp.status_code == 200
    task_id = task_resp.json()["id"]
    assert task_resp.json()["prompt_template_id"] == prompt_id

    with Session(get_engine()) as session:
        task_row = session.get(GenerationTask, task_id)
        assert task_row is not None
        task_row.status = "reviewed"
        session.add(task_row)
        draft = CaseDraft(
            task_id=task_id,
            version=1,
            content_md="## TC-001\n- 优先级：P1",
            prompt_version_ref=f"id:{prompt_id}:v1",
        )
        session.add(draft)
        session.flush()
        session.add(
            ReviewResult(
                task_id=task_id,
                draft_id=int(draft.id),
                score=80,
                verdict="revise",
                payload_json='{"issues":["补充边界"]}',
            )
        )
        rev = PromptRevision(
            task_id=task_id,
            base_prompt_id=prompt_id,
            new_content="SHARED_V1_OPTIMIZED",
            status="pending",
        )
        session.add(rev)
        session.commit()
        rev_id = int(rev.id)

    # Alice switches the prompt back to private and edits in sensitive content.
    downgraded = alice.put(
        f"/api/prompts/{prompt_id}",
        headers=_origin(),
        json={"visibility": "private", "content": "SECRET_AFTER_PRIVATE_DOWNGRADE"},
    )
    assert downgraded.status_code == 200
    assert downgraded.json()["visibility"] == "private"

    leaked_calls: list[str] = []
    monkeypatch.setattr("app.api.tasks._OPTIMIZE_CHAT_FN", lambda **kw: leaked_calls.append(str(kw)) or "x")
    monkeypatch.setattr("app.services.task_pipeline._OPTIMIZE_CHAT_FN", lambda **kw: leaked_calls.append(str(kw)) or "x")
    monkeypatch.setattr("app.api.tasks._GENERATE_CHAT_FN", lambda **kw: leaked_calls.append(str(kw)) or "x")
    monkeypatch.setattr("app.services.task_pipeline._GENERATE_CHAT_FN", lambda **kw: leaked_calls.append(str(kw)) or "x")

    # Admin can no longer see the prompt id, revisions, or draft prompt ref.
    assert admin.get(f"/api/tasks/{task_id}", headers=_origin()).json()["prompt_template_id"] is None
    assert admin.get(f"/api/tasks/{task_id}/revisions", headers=_origin()).json() == []
    assert admin.get(f"/api/tasks/{task_id}/drafts", headers=_origin()).json()[0]["prompt_version_ref"] == "private"

    # Admin is blocked with 403 on optimize-prompt / regenerate / apply-prompt / generate.
    assert admin.post(f"/api/tasks/{task_id}/optimize-prompt", headers=_origin()).status_code == 403
    assert admin.post(f"/api/tasks/{task_id}/regenerate", headers=_origin()).status_code == 403
    assert admin.post(f"/api/tasks/{task_id}/generate", headers=_origin()).status_code == 403
    assert admin.post(
        f"/api/tasks/{task_id}/apply-prompt",
        headers=_origin(),
        json={"revision_id": rev_id, "mode": "task_temp"},
    ).status_code == 403
    assert leaked_calls == []


# ---------------------------------------------------------------------------
# 10. Prompt API hardening: engine POST visibility 422, PUT last-system 409, orphan guard
# ---------------------------------------------------------------------------


def test_prompt_api_hardening_engine_visibility_and_last_system_protection(tmp_app_data, monkeypatch):
    admin, _ = _setup_auth_env(monkeypatch)

    # POST engine prompt with non-system visibility returns 422 (consistent with PUT).
    for vis in ("private", "shared"):
        resp = admin.post(
            "/api/prompts",
            headers=_origin(),
            json={
                "name": f"bad-review-{vis}",
                "type": "review",
                "content": "review body",
                "visibility": vis,
            },
        )
        assert resp.status_code == 422

    # PUT cannot deactivate or change visibility of the last active system generate prompt (409).
    sys_gens = admin.get(
        "/api/prompts",
        params={"type": "generate", "visibility": "system"},
        headers=_origin(),
    ).json()
    active_sys = [p for p in sys_gens if p["is_active"]]
    assert len(active_sys) == 1
    only_sys_id = active_sys[0]["id"]

    assert admin.put(
        f"/api/prompts/{only_sys_id}",
        headers=_origin(),
        json={"is_active": False},
    ).status_code == 409
    assert admin.put(
        f"/api/prompts/{only_sys_id}",
        headers=_origin(),
        json={"visibility": "private"},
    ).status_code == 409
    assert admin.put(
        f"/api/prompts/{only_sys_id}",
        headers=_origin(),
        json={"visibility": "shared"},
    ).status_code == 409

    # Once a second active system generate prompt exists, the first can be updated cleanly.
    second = admin.post(
        "/api/prompts",
        headers=_origin(),
        json={
            "name": "second-sys-gen",
            "type": "generate",
            "content": "second system prompt",
            "visibility": "system",
            "is_active": True,
        },
    )
    assert second.status_code == 200
    assert admin.put(
        f"/api/prompts/{only_sys_id}",
        headers=_origin(),
        json={"is_active": False},
    ).status_code == 200

    # Orphan defense: an unowned shared prompt (owner_user_id=None) changed to private by admin
    # gets assigned owner_user_id = admin.id so it is never orphaned.
    with Session(get_engine()) as session:
        unowned_shared = PromptTemplate(
            name="legacy-unowned-shared",
            type="generate",
            content="legacy shared",
            version=50,
            is_active=True,
            owner_user_id=None,
            visibility="shared",
        )
        session.add(unowned_shared)
        session.commit()
        session.refresh(unowned_shared)
        unowned_id = int(unowned_shared.id)

    adopted = admin.put(
        f"/api/prompts/{unowned_id}",
        headers=_origin(),
        json={"visibility": "private"},
    )
    assert adopted.status_code == 200
    assert adopted.json()["visibility"] == "private"
    assert adopted.json()["owner_user_id"] is not None
    assert adopted.json()["owner_username"] == "admin"
    assert admin.get(f"/api/prompts/{unowned_id}", headers=_origin()).status_code == 200


# ---------------------------------------------------------------------------
# 11. Migration 0004 & API filtering for retired wiki_analyze / wiki_write prompts
# ---------------------------------------------------------------------------


def test_migration_0004_removes_retired_wiki_prompts_and_deactivates_referenced(tmp_app_data):
    db_path = tmp_app_data / "meta" / "app.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE prompt_templates (
                id INTEGER PRIMARY KEY,
                name VARCHAR NOT NULL,
                type VARCHAR NOT NULL,
                content VARCHAR NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                owner_user_id INTEGER,
                visibility VARCHAR NOT NULL DEFAULT 'system',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE generation_tasks (
                id INTEGER PRIMARY KEY,
                requirement_id INTEGER NOT NULL,
                wiki_space_id INTEGER,
                status VARCHAR NOT NULL,
                prompt_template_id INTEGER,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE prompt_revisions (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL,
                base_prompt_id INTEGER,
                new_content VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO prompt_templates (id, name, type, content, version, is_active, visibility)
            VALUES
                (1, 'default_generate', 'generate', 'gen body', 10, 1, 'system'),
                (2, 'default_review', 'review', 'rev body', 10, 1, 'system'),
                (3, 'default_wiki_analyze', 'wiki_analyze', 'old analyze', 1, 1, 'system'),
                (4, 'default_wiki_write', 'wiki_write', 'old write', 1, 1, 'system'),
                (5, 'ref_wiki_write', 'wiki_write', 'referenced by task', 2, 1, 'system'),
                (6, 'ref_wiki_analyze', 'wiki_analyze', 'referenced by rev', 2, 1, 'system')
            """
        )
        conn.execute(
            "INSERT INTO generation_tasks (id, requirement_id, status, prompt_template_id) "
            "VALUES (10, 1, 'draft', 5)"
        )
        conn.execute(
            "INSERT INTO prompt_revisions (id, task_id, base_prompt_id, new_content, status) "
            "VALUES (20, 10, 6, 'rev', 'pending')"
        )
        conn.commit()

    reset_engine()
    init_db()
    # Second init_db must stay idempotent
    init_db()

    applied = get_applied_migrations(get_engine())
    assert "0004_remove_legacy_wiki_prompts" in applied

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, type, is_active FROM prompt_templates ORDER BY id"
        ).fetchall()
    by_id = {int(r[0]): (str(r[1]), int(r[2])) for r in rows}
    # Valid rows remain intact
    assert by_id[1] == ("generate", 1)
    assert by_id[2] == ("review", 1)
    # Unreferenced retired wiki prompts are deleted
    assert 3 not in by_id
    assert 4 not in by_id
    # Referenced retired wiki prompts are preserved but deactivated for integrity
    assert by_id[5] == ("wiki_write", 0)
    assert by_id[6] == ("wiki_analyze", 0)


def test_retired_prompt_types_hidden_from_list_and_404_on_single_item_apis(tmp_app_data, monkeypatch):
    admin, project_id = _setup_auth_env(monkeypatch)
    alice, _ = _create_user_and_login("alice", project_id)

    # Inject retired / non-PROMPT_TYPES rows directly to verify API-layer defense in depth.
    retired_ids: list[int] = []
    with Session(get_engine()) as session:
        for name, ptype in (
            ("default_wiki_analyze", "wiki_analyze"),
            ("default_wiki_write", "wiki_write"),
            ("legacy_unknown", "retired_legacy_type"),
        ):
            row = PromptTemplate(
                name=name,
                type=ptype,
                content="retired content",
                version=1,
                is_active=True,
                owner_user_id=None,
                visibility="system",
            )
            session.add(row)
            session.flush()
            retired_ids.append(int(row.id))
        session.commit()

    for actor_name, client in (("admin", admin), ("alice", alice)):
        listed = client.get("/api/prompts", headers=_origin())
        assert listed.status_code == 200
        returned_types = {item["type"] for item in listed.json()}
        returned_ids = {item["id"] for item in listed.json()}
        assert "wiki_analyze" not in returned_types, f"{actor_name} saw wiki_analyze"
        assert "wiki_write" not in returned_types, f"{actor_name} saw wiki_write"
        assert "retired_legacy_type" not in returned_types
        assert not (set(retired_ids) & returned_ids)

        for ptype in ("wiki_analyze", "wiki_write", "retired_legacy_type"):
            filtered = client.get("/api/prompts", params={"type": ptype}, headers=_origin())
            assert filtered.status_code == 200
            assert filtered.json() == []

        for rid in retired_ids:
            r_get = client.get(f"/api/prompts/{rid}", headers=_origin())
            assert r_get.status_code == 404
            assert r_get.json()["detail"] == "Prompt not found"

            r_put = client.put(f"/api/prompts/{rid}", headers=_origin(), json={"content": "edit"})
            assert r_put.status_code == 404
            assert r_put.json()["detail"] == "Prompt not found"

            r_del = client.delete(f"/api/prompts/{rid}", headers=_origin())
            assert r_del.status_code == 404
            assert r_del.json()["detail"] == "Prompt not found"

            r_fork = client.post(f"/api/prompts/{rid}/fork", headers=_origin(), json={})
            assert r_fork.status_code == 404
            assert r_fork.json()["detail"] == "Prompt not found"


def test_list_prompts_orders_active_before_inactive(tmp_app_data, monkeypatch):
    client, _ = _setup_auth_env(monkeypatch)

    res_inactive = client.post(
        "/api/prompts",
        headers=_origin(),
        json={
            "name": "gen_inactive_newer",
            "type": "generate",
            "content": "inactive template",
            "visibility": "system",
            "is_active": False,
        },
    )
    assert res_inactive.status_code == 200, res_inactive.text

    res_active = client.post(
        "/api/prompts",
        headers=_origin(),
        json={
            "name": "gen_active_older",
            "type": "generate",
            "content": "active template",
            "visibility": "system",
            "is_active": True,
        },
    )
    assert res_active.status_code == 200, res_active.text

    # 再建一条较新的未启用模板（id 更大），列表仍应把所有启用项排在未启用项之前
    res_inactive_newest = client.post(
        "/api/prompts",
        headers=_origin(),
        json={
            "name": "gen_inactive_newest",
            "type": "generate",
            "content": "inactive newest template",
            "visibility": "system",
            "is_active": False,
        },
    )
    assert res_inactive_newest.status_code == 200, res_inactive_newest.text

    listing = client.get("/api/prompts?type=generate", headers=_origin())
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    active_flags = [row["is_active"] for row in rows]
    assert True in active_flags and False in active_flags
    first_inactive_idx = active_flags.index(False)
    assert all(flag is True for flag in active_flags[:first_inactive_idx])
    assert all(flag is False for flag in active_flags[first_inactive_idx:])

