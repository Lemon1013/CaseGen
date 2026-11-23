"""Project boundary, WikiSpace compatibility shell, and knowledge-source count tests."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import _migrate_project_schema, get_engine
from app.main import create_app
from app.models.entities import (
    DataPool,
    ExampleVariant,
    GenerationTask,
    ManagedPlatformCase,
    PlatformExample,
    PlatformProfile,
    PlatformRenderRun,
    Project,
    Requirement,
    User,
    WikiSpace,
)
from app.models.external_wiki import ProjectKnowledgeSource
from app.services.auth import hash_password


def test_legacy_space_backfill_creates_project_without_making_it_shared(tmp_app_data):
    TestClient(create_app())
    with Session(get_engine()) as session:
        legacy = WikiSpace(name="Legacy", slug="legacy", scope="project", project_id=None)
        session.add(legacy)
        session.commit()
        session.refresh(legacy)
        legacy_id = int(legacy.id)
    _migrate_project_schema(get_engine(), backfill=True)
    with Session(get_engine()) as session:
        legacy = session.get(WikiSpace, legacy_id)
        project = session.get(Project, legacy.project_id)
        assert legacy.scope == "project"
        assert project.slug == "legacy"
        assert project.default_wiki_space_id == legacy_id


def test_normal_project_backfill_twice_keeps_owner_and_project_count(tmp_app_data):
    client = TestClient(create_app())
    project = client.post("/api/projects", json={"name": "Stable", "slug": "stable"}).json()
    before = len(client.get("/api/projects").json())
    _migrate_project_schema(get_engine(), backfill=True)
    _migrate_project_schema(get_engine(), backfill=True)
    with Session(get_engine()) as session:
        assert len(session.exec(select(Project)).all()) == before
        space = session.get(WikiSpace, project["default_wiki_space_id"])
        assert space.project_id == project["id"]
        assert session.get(Project, project["id"]).default_wiki_space_id == space.id


def test_project_backfill_is_idempotent_and_repairs_strict_duplicate(tmp_app_data):
    client = TestClient(create_app())
    canonical = client.post("/api/projects", json={"name": "Repair", "slug": "repair"}).json()
    outsider = client.post("/api/projects", json={"name": "Outside", "slug": "outside"}).json()
    space_id = canonical["default_wiki_space_id"]
    with Session(get_engine()) as session:
        duplicate = Project(name="Repair Wiki", slug="repair-wiki", default_wiki_space_id=space_id)
        session.add(duplicate)
        session.flush()
        duplicate_id = int(duplicate.id)
        space = session.get(WikiSpace, space_id)
        space.project_id = duplicate_id
        requirement = Requirement(project_id=duplicate_id, title="R", description="D")
        session.add(requirement)
        session.flush()
        task = GenerationTask(
            requirement_id=int(requirement.id),
            project_id=duplicate_id,
            wiki_space_id=space_id,
            status="draft",
        )
        session.add(task)
        session.commit()
        requirement_id, task_id = int(requirement.id), int(task.id)

    _migrate_project_schema(get_engine(), backfill=True)
    _migrate_project_schema(get_engine(), backfill=True)
    with Session(get_engine()) as session:
        assert len(session.exec(select(Project)).all()) == 3  # default, canonical, outsider
        assert session.get(Project, duplicate_id) is None
        assert session.get(Project, canonical["id"]).default_wiki_space_id == space_id
        assert session.get(WikiSpace, space_id).project_id == canonical["id"]
        assert session.get(Requirement, requirement_id).project_id == canonical["id"]
        assert session.get(GenerationTask, task_id).project_id == canonical["id"]
    for path in ("/api/data-pools", "/api/platforms", "/api/platform-cases"):
        assert client.get(path, params={"project_id": canonical["id"], "wiki_space_id": space_id}).status_code == 200
        assert client.get(path, params={"project_id": outsider["id"], "wiki_space_id": space_id}).status_code == 404


def test_project_scope_rejects_cross_project_resources_consistently(tmp_app_data):
    client = TestClient(create_app())
    owner = client.post("/api/projects", json={"name": "Owner", "slug": "owner"}).json()
    outsider = client.post("/api/projects", json={"name": "Outsider", "slug": "outsider"}).json()
    space_id = owner["default_wiki_space_id"]
    with Session(get_engine()) as session:
        pool = DataPool(wiki_space_id=space_id, name="pool")
        platform = PlatformProfile(wiki_space_id=space_id, name="platform")
        source = ProjectKnowledgeSource(
            project_id=owner["id"],
            name="owner-kb",
            provider_type="llm_wiki_django",
            base_url="http://127.0.0.1:8091",
            auth_type="none",
        )
        session.add(pool)
        session.add(platform)
        session.add(source)
        session.flush()
        variant = ExampleVariant(platform_id=int(platform.id), name="variant")
        session.add(variant)
        session.flush()
        example = PlatformExample(variant_id=int(variant.id), kind="combined", content="{}", content_hash="x")
        run = PlatformRenderRun(wiki_space_id=space_id, platform_id=int(platform.id), variant_id=int(variant.id))
        case = ManagedPlatformCase(
            wiki_space_id=space_id,
            platform_id=int(platform.id),
            variant_id=int(variant.id),
            filename="case.json",
            kind="combined",
            content="{}",
            content_hash="x",
        )
        session.add(example)
        session.add(run)
        session.add(case)
        session.commit()
        ids = {
            name: int(row.id)
            for name, row in {
                "pool": pool,
                "platform": platform,
                "variant": variant,
                "example": example,
                "run": run,
                "case": case,
                "source": source,
            }.items()
        }

    project_query = {"project_id": outsider["id"]}
    requests = [
        ("get", f"/api/wiki-spaces/{space_id}", None),
        ("put", f"/api/wiki-spaces/{space_id}", {"name": "blocked"}),
        ("get", f"/api/data-pools/{ids['pool']}", None),
        ("post", f"/api/data-pools/{ids['pool']}/archive", None),
        ("patch", f"/api/platforms/{ids['platform']}", {"name": "blocked"}),
        ("post", f"/api/platforms/{ids['platform']}/variants", {"name": "blocked"}),
        ("patch", f"/api/platforms/{ids['platform']}/variants/{ids['variant']}", {"name": "blocked"}),
        ("patch", f"/api/platforms/{ids['platform']}/variants/{ids['variant']}/examples/{ids['example']}", {"name": "blocked"}),
        ("get", f"/api/platform-cases/{ids['case']}", None),
        ("patch", f"/api/platform-cases/{ids['case']}", {"name": "blocked"}),
        ("delete", f"/api/platform-cases/{ids['case']}", None),
        ("post", f"/api/platform-cases/{ids['case']}/restore", None),
        ("get", f"/api/platform-renders/{ids['run']}", None),
        ("get", f"/api/projects/{outsider['id']}/knowledge-sources/{ids['source']}/files", None),
    ]
    for method, url, body in requests:
        response = client.request(method, url, params=project_query, json=body)
        assert response.status_code == 404, (method, url, response.text)


def test_unauthenticated_project_scope_never_reveals_membership(tmp_app_data, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    client = TestClient(create_app())
    with Session(get_engine()) as session:
        project = session.exec(select(Project)).first()
        space_id = int(project.default_wiki_space_id)
        project_id = int(project.id)

    matching = client.get(f"/api/wiki-spaces/{space_id}", params={"project_id": project_id})
    mismatching = client.get(f"/api/wiki-spaces/{space_id}", params={"project_id": project_id + 999})
    assert matching.status_code == mismatching.status_code == 401
    assert matching.json() == mismatching.json() == {"detail": "Not authenticated"}


def test_authenticated_project_context_is_required_and_matches_targets(tmp_app_data, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    client = TestClient(create_app())
    origin = {"Origin": "http://testserver"}
    assert client.post(
        "/api/auth/setup",
        headers=origin,
        json={"username": "admin", "password": "CaseGen-Test1!"},
    ).status_code == 200
    with Session(get_engine()) as session:
        first = session.exec(select(Project)).first()
        first_id, first_space = int(first.id), int(first.default_wiki_space_id)
    second = client.post("/api/projects", headers=origin, json={"name": "Second", "slug": "second-auth"}).json()
    second_id, second_space = second["id"], second["default_wiki_space_id"]

    assert client.get("/api/requirements").status_code == 422
    assert client.get(f"/api/wiki-spaces/{first_space}").status_code == 422
    assert client.post(
        "/api/requirements",
        headers=origin,
        json={"project_id": first_id, "title": "missing", "description": "missing"},
    ).status_code == 422
    assert client.put(f"/api/projects/{first_id}", headers=origin, json={"name": "missing"}).status_code == 422

    first_query = {"project_id": first_id}
    assert client.post(
        "/api/requirements",
        params=first_query,
        headers=origin,
        json={"project_id": second_id, "title": "cross", "description": "cross"},
    ).status_code == 404
    assert client.post(
        "/api/wiki-spaces",
        params=first_query,
        headers=origin,
        json={"name": "cross", "slug": "cross-space", "scope": "project", "project_id": second_id},
    ).status_code == 404
    assert client.post(
        "/api/tasks",
        params=first_query,
        headers=origin,
        json={"title": "cross", "description": "cross", "project_id": second_id, "wiki_space_id": second_space},
    ).status_code == 404
    assert client.post(
        "/api/data-pools",
        params=first_query,
        headers=origin,
        json={"wiki_space_id": second_space, "name": "cross", "source_kind": "json", "content": "[]"},
    ).status_code == 422

    created = client.post(
        "/api/requirements",
        params=first_query,
        headers=origin,
        json={"project_id": first_id, "title": "same", "description": "same"},
    )
    assert created.status_code == 200
    private = client.post(
        "/api/wiki-spaces",
        params=first_query,
        headers=origin,
        json={"name": "same", "slug": "same-space", "scope": "project"},
    )
    assert private.status_code == 201
    assert private.json()["project_id"] == first_id

    with Session(get_engine()) as session:
        session.add(
            User(
                username="member2",
                display_name="Member",
                password_hash=hash_password("CaseGen-Test1!"),
                role="user",
                is_active=True,
            )
        )
        session.commit()
    client.post("/api/auth/logout", headers=origin)
    assert client.post(
        "/api/auth/login",
        headers=origin,
        json={"username": "member2", "password": "CaseGen-Test1!"},
    ).status_code == 200
    assert client.put(f"/api/projects/{first_id}", params=first_query, headers=origin, json={"name": "blocked"}).status_code == 403
    assert client.delete(f"/api/projects/{first_id}", params=first_query, headers=origin).status_code == 403


def test_empty_project_delete_removes_private_space_cleanly(tmp_app_data):
    client = TestClient(create_app())
    project = client.post("/api/projects", json={"name": "Disposable", "slug": "disposable"}).json()

    assert client.delete(f"/api/projects/{project['id']}").status_code == 204
    with Session(get_engine()) as session:
        assert session.get(Project, project["id"]) is None
        assert session.get(WikiSpace, project["default_wiki_space_id"]) is None


def test_project_delete_and_archive_safety_guards(tmp_app_data):
    client = TestClient(create_app())
    default = next(item for item in client.get("/api/projects").json() if item["slug"] == "default")
    assert client.delete(f"/api/projects/{default['id']}").status_code == 409
    assert client.put(f"/api/projects/{default['id']}", json={"status": "archived"}).status_code == 409

    busy = client.post("/api/projects", json={"name": "Busy", "slug": "busy"}).json()
    with Session(get_engine()) as session:
        session.add(Requirement(project_id=busy["id"], title="R", description="D"))
        session.commit()
    assert client.delete(f"/api/projects/{busy['id']}").status_code == 409

    last = client.post("/api/projects", json={"name": "Last active", "slug": "last-active"}).json()
    with Session(get_engine()) as session:
        for row in session.exec(select(Project).where(Project.id != last["id"])).all():
            row.status = "archived"
            session.add(row)
        session.commit()
    assert client.delete(f"/api/projects/{last['id']}").status_code == 409
    assert client.put(f"/api/projects/{last['id']}", json={"status": "archived"}).status_code == 409


def test_requirement_create_rejects_missing_or_inactive_project(tmp_app_data):
    client = TestClient(create_app())
    project = client.post("/api/projects", json={"name": "Inactive", "slug": "inactive"}).json()
    assert client.put(f"/api/projects/{project['id']}", json={"status": "archived"}).status_code == 200
    assert client.post("/api/requirements", json={"project_id": project["id"], "title": "R", "description": "D"}).status_code == 422
    assert client.post("/api/requirements", json={"project_id": 999999, "title": "R", "description": "D"}).status_code == 422


def test_active_project_default_wiki_cannot_be_archived(tmp_app_data):
    client = TestClient(create_app())
    project = client.post("/api/projects", json={"name": "Default guard", "slug": "default-guard"}).json()
    space_id = project["default_wiki_space_id"]
    assert client.post(f"/api/wiki-spaces/{space_id}/archive").status_code == 409
    assert client.patch(f"/api/wiki-spaces/{space_id}/status", json={"status": "archived"}).status_code == 409


def test_project_out_counts_configured_knowledge_sources(tmp_app_data):
    client = TestClient(create_app())
    project = client.post("/api/projects", json={"name": "KS Count", "slug": "ks-count"}).json()
    assert project["knowledge_source_count"] == 0

    with Session(get_engine()) as session:
        session.add_all(
            [
                ProjectKnowledgeSource(
                    project_id=project["id"],
                    name="已启用且配置Scope",
                    provider_type="llm_wiki_django",
                    base_url="http://127.0.0.1:8091",
                    config_json=json.dumps(
                        {"external_project_id": "proj-1", "external_project_name": "交易规则"}
                    ),
                    priority=20,
                    enabled=True,
                ),
                ProjectKnowledgeSource(
                    project_id=project["id"],
                    name="更高优先级知识库",
                    provider_type="llm_wiki_django",
                    base_url="http://127.0.0.1:8091",
                    config_json=json.dumps(
                        {"external_project_id": "proj-0", "external_project_name": "清算规则"}
                    ),
                    priority=10,
                    enabled=True,
                ),
                ProjectKnowledgeSource(
                    project_id=project["id"],
                    name="已启用但未配Scope",
                    provider_type="llm_wiki_django",
                    base_url="http://127.0.0.1:8091",
                    config_json=json.dumps({"external_project_id": ""}),
                    enabled=True,
                ),
                ProjectKnowledgeSource(
                    project_id=project["id"],
                    name="已禁用",
                    provider_type="llm_wiki_django",
                    base_url="http://127.0.0.1:8091",
                    config_json=json.dumps({"external_project_id": "proj-2"}),
                    enabled=False,
                ),
            ]
        )
        session.commit()

    refreshed = client.get(f"/api/projects/{project['id']}").json()
    assert refreshed["knowledge_source_count"] == 2
    assert [item["name"] for item in refreshed["knowledge_sources"]] == [
        "更高优先级知识库",
        "已启用且配置Scope",
    ]
    assert [item["external_project_name"] for item in refreshed["knowledge_sources"]] == [
        "清算规则",
        "交易规则",
    ]
