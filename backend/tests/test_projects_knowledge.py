import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import _migrate_project_schema, get_engine
from app.main import create_app
from app import config
from app.models.entities import Document, GenerationTask, IngestJob, Project, ProjectKnowledgeDecision, ProjectWikiBinding, Requirement, TaskCitation, TaskKnowledgeDecision, User, WikiPageRevision, WikiPageRow, WikiReviewItem, WikiSpace
from app.services.auth import hash_password
from app.services.wiki_repository import WikiRepository
from app.services.wiki_schema import WikiFrontmatter, WikiPage


def _add_page(session: Session, space: WikiSpace, key: str, assertion: str) -> int:
    document = Document(filename=f"{key}.md", stored_path=f"raw/{key}.md", content_type="text/markdown", sha256=key.ljust(64, "0")[:64], status="ready", space_id=space.id)
    session.add(document); session.flush()
    page = WikiPage(frontmatter=WikiFrontmatter(page_key=key, title="取消规则", type="rule", domain="orders", canonical_topic="order.cancel.after-payment", assertion_summary=assertion, sources=[{"document_id": document.id}], status="published"), body=f"订单取消规则：{assertion}")
    return int(WikiRepository(session, space_id=space.id).create(page, reason="test").row.id)


def test_projects_shared_binding_conflict_and_decision(tmp_app_data):
    client = TestClient(create_app())
    default = next(item for item in client.get("/api/wiki-spaces").json() if item["slug"] == "default")
    assert default["scope"] == "project"

    project = client.post("/api/projects", json={"name": "订单项目", "slug": "orders"}).json()
    other = client.post("/api/projects", json={"name": "其他项目", "slug": "other"}).json()
    shared = client.post("/api/wiki-spaces", json={"name": "公司规则", "slug": "company-rules", "scope": "shared", "namespace": "company"})
    assert shared.status_code == 201
    shared = shared.json()
    assert client.put(f"/api/projects/{project['id']}/shared-wikis/{shared['id']}", json={"wiki_space_id": shared["id"], "priority": 10, "enabled": True}).status_code == 200

    private_id = project["default_wiki_space_id"]
    with Session(get_engine()) as session:
        private = session.get(WikiSpace, private_id)
        public = session.get(WikiSpace, shared["id"])
        private_page = _add_page(session, private, "order.cancel", "支付后 30 分钟内允许取消")
        public_page = _add_page(session, public, "company.order-cancel", "支付后不允许取消")
        session.commit()
        prefixed_page = _add_page(session, public, "auto-prefix", "确定性前缀")
        session.commit()
        assert session.get(WikiPageRow, prefixed_page).page_key == "company.auto-prefix"

    result = client.post("/api/wiki/retrieve", json={"query": "订单取消规则", "project_id": project["id"], "space_id": private_id, "top_k": 20})
    assert result.status_code == 200
    conflict = result.json()["conflict_groups"][0]
    assert {private_page, public_page}.issubset({item["id"] for item in conflict["candidates"]})
    assert {item["space_scope"] for item in conflict["candidates"]} == {"project", "shared"}

    illegal = client.post("/api/wiki/retrieve", json={"query": "规则", "project_id": other["id"], "shared_space_ids": [shared["id"]]})
    assert illegal.status_code == 422

    task = client.post("/api/tasks", json={"title": "取消测试", "description": "验证取消", "project_id": project["id"], "wiki_space_id": private_id}).json()
    # Decisions cannot be planted before this task has a pending candidate snapshot.
    early = client.put(f"/api/tasks/{task['id']}/knowledge-decisions/order.cancel.after-payment", json={"checkpoint_id": 1, "checkpoint_version": 1, "conflict_key": "order.cancel.after-payment", "selected_page_id": private_page})
    assert early.status_code == 409
    from app.models.entities import TaskRetrievalCheckpoint
    import json
    with Session(get_engine()) as session:
        task_row = session.get(GenerationTask, task["id"]); task_row.status = "awaiting_confirmation"; session.add(task_row)
        first_citation = TaskCitation(task_id=task["id"], wiki_page_id=private_page, title="项目", path="project.md")
        second_citation = TaskCitation(task_id=task["id"], wiki_page_id=public_page, title="公共", path="shared.md")
        session.add(first_citation); session.add(second_citation); session.flush()
        checkpoint = TaskRetrievalCheckpoint(task_id=task["id"], status="pending", query="取消", retrieval_json=json.dumps({"conflict_groups": [conflict]}), candidate_citation_ids_json=json.dumps([first_citation.id, second_citation.id]))
        session.add(checkpoint); session.commit(); session.refresh(checkpoint)
        checkpoint_id = int(checkpoint.id)
        first_citation_id, second_citation_id = int(first_citation.id), int(second_citation.id)
    saved = client.put(f"/api/tasks/{task['id']}/knowledge-decisions/order.cancel.after-payment", json={"checkpoint_id": checkpoint_id, "checkpoint_version": 1, "conflict_key": "order.cancel.after-payment", "selected_page_id": private_page, "decision_scope": "once", "reason": "项目例外"})
    assert saved.status_code == 200
    confirmed = client.post(f"/api/tasks/{task['id']}/retrieval-checkpoint/confirm", json={"selected_citation_ids": [first_citation_id, second_citation_id], "supplemental_text": "", "expected_version": 1, "idempotency_key": "conflict-filter"})
    assert confirmed.status_code == 200
    with Session(get_engine()) as session:
        row = session.exec(select(TaskKnowledgeDecision).where(TaskKnowledgeDecision.task_id == task["id"])).one()
        assert row.selected_page_id == private_page
        assert row.selected_revision == 1
        checkpoint = session.get(TaskRetrievalCheckpoint, checkpoint_id)
        assert json.loads(checkpoint.selected_citation_ids_json) == [first_citation_id]

    bad_task = client.post("/api/tasks", json={"title": "其他", "description": "其他", "project_id": other["id"], "wiki_space_id": other["default_wiki_space_id"]}).json()
    rejected = client.put(f"/api/tasks/{bad_task['id']}/knowledge-decisions/order.cancel.after-payment", json={"checkpoint_id": checkpoint_id, "checkpoint_version": 1, "conflict_key": "order.cancel.after-payment", "selected_page_id": public_page})
    assert rejected.status_code == 409

    proposal = client.post(f"/api/wiki/pages/{private_page}/propose-update", params={"space_id": private_id}, json={"assertion_summary": "支付后 60 分钟内允许取消", "reason": "项目规则修订"})
    assert proposal.status_code == 200
    approved = client.post(f"/api/wiki/reviews/{proposal.json()['id']}/approve", params={"space_id": private_id}, json={"reason": "确认修订"})
    assert approved.status_code == 200
    page = client.get(f"/api/wiki/pages/{private_page}", params={"space_id": private_id}).json()
    assert page["canonical_topic"] == "order.cancel.after-payment"
    assert page["assertion_summary"] == "支付后 60 分钟内允许取消"
    assert client.post(f"/api/wiki-spaces/{shared['id']}/archive").status_code == 200
    assert client.put(f"/api/projects/{other['id']}/shared-wikis/{shared['id']}", json={"wiki_space_id": shared["id"], "priority": 10, "enabled": True}).status_code == 422


def test_legacy_space_backfill_creates_project_without_making_it_shared(tmp_app_data):
    TestClient(create_app())
    with Session(get_engine()) as session:
        legacy = WikiSpace(name="Legacy", slug="legacy", scope="project", project_id=None)
        session.add(legacy); session.commit(); session.refresh(legacy)
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
        session.add(duplicate); session.flush()
        duplicate_id = int(duplicate.id)
        space = session.get(WikiSpace, space_id); space.project_id = duplicate_id
        requirement = Requirement(project_id=duplicate_id, title="R", description="D")
        session.add(requirement); session.flush()
        task = GenerationTask(requirement_id=int(requirement.id), project_id=duplicate_id, wiki_space_id=space_id, status="draft")
        session.add(task); session.commit()
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


def _make_strict_duplicate(session: Session, slug: str):
    canonical = Project(name=slug, slug=slug)
    session.add(canonical); session.flush()
    space = WikiSpace(name=slug, slug=f"{slug}-wiki", project_id=int(canonical.id))
    session.add(space); session.flush()
    duplicate = Project(name=f"{slug} wiki", slug=f"{slug}-wiki", default_wiki_space_id=int(space.id))
    session.add(duplicate); session.flush()
    canonical.default_wiki_space_id = int(space.id)
    space.project_id = int(duplicate.id)
    session.commit()
    return int(canonical.id), int(duplicate.id), int(space.id)


def test_project_backfill_binding_conflict_rolls_back(tmp_app_data):
    TestClient(create_app())
    with Session(get_engine()) as session:
        canonical_id, duplicate_id, space_id = _make_strict_duplicate(session, "binding-repair")
        shared = WikiSpace(name="Shared", slug="binding-shared", scope="shared", namespace="binding")
        session.add(shared); session.flush()
        session.add(ProjectWikiBinding(project_id=canonical_id, wiki_space_id=int(shared.id), priority=10, enabled=True))
        session.add(ProjectWikiBinding(project_id=duplicate_id, wiki_space_id=int(shared.id), priority=20, enabled=True))
        session.commit()
    with pytest.raises(RuntimeError, match="conflicting shared wiki binding"):
        _migrate_project_schema(get_engine(), backfill=True)
    with Session(get_engine()) as session:
        assert session.get(Project, duplicate_id) is not None
        assert session.get(WikiSpace, space_id).project_id == duplicate_id


def test_project_backfill_decision_conflict_rolls_back(tmp_app_data):
    TestClient(create_app())
    with Session(get_engine()) as session:
        canonical_id, duplicate_id, space_id = _make_strict_duplicate(session, "decision-repair")
        session.add(ProjectKnowledgeDecision(project_id=canonical_id, conflict_key="topic", selected_page_id=1, selected_revision=1))
        session.add(ProjectKnowledgeDecision(project_id=duplicate_id, conflict_key="topic", selected_page_id=2, selected_revision=1))
        session.commit()
    with pytest.raises(RuntimeError, match="conflicting knowledge decision"):
        _migrate_project_schema(get_engine(), backfill=True)
    with Session(get_engine()) as session:
        assert session.get(Project, duplicate_id) is not None
        assert session.get(WikiSpace, space_id).project_id == duplicate_id


def test_project_backfill_decision_audit_conflict_rolls_back(tmp_app_data):
    TestClient(create_app())
    with Session(get_engine()) as session:
        canonical_id, duplicate_id, space_id = _make_strict_duplicate(session, "decision-audit-repair")
        session.add(ProjectKnowledgeDecision(project_id=canonical_id, conflict_key="topic", selected_page_id=1, selected_revision=1, decided_by="alice", reason="reviewed"))
        session.add(ProjectKnowledgeDecision(project_id=duplicate_id, conflict_key="topic", selected_page_id=1, selected_revision=1, decided_by="bob", reason="imported"))
        session.commit()
    with pytest.raises(RuntimeError, match="conflicting knowledge decision"):
        _migrate_project_schema(get_engine(), backfill=True)
    with Session(get_engine()) as session:
        assert session.get(Project, duplicate_id) is not None
        assert session.get(WikiSpace, space_id).project_id == duplicate_id


def test_shared_write_endpoints_require_admin(tmp_app_data, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    client = TestClient(create_app()); origin = {"Origin": "http://testserver"}
    assert client.post("/api/auth/setup", headers=origin, json={"username": "admin", "password": "password1234"}).status_code == 200
    with Session(get_engine()) as session:
        default_project = session.exec(select(Project)).first()
        client.params = {"project_id": int(default_project.id)}
    shared = client.post("/api/wiki-spaces", headers=origin, json={"name": "Shared", "slug": "shared", "scope": "shared", "namespace": "shared"}).json()
    assert client.put(
        f"/api/projects/{default_project.id}/shared-wikis/{shared['id']}",
        headers=origin,
        json={"wiki_space_id": shared["id"], "enabled": True, "priority": 100},
    ).status_code == 200
    with Session(get_engine()) as session:
        session.add(User(username="member", display_name="Member", password_hash=hash_password("password1234"), role="user", is_active=True)); session.commit()
    client.post("/api/auth/logout", headers=origin); assert client.post("/api/auth/login", headers=origin, json={"username": "member", "password": "password1234"}).status_code == 200
    assert client.put(f"/api/wiki-spaces/{shared['id']}", headers=origin, json={"name": "Blocked"}).status_code == 403
    assert client.post("/api/documents", headers=origin, data={"space_id": shared["id"]}, files={"file": ("x.md", "x", "text/markdown")}).status_code == 403
    with Session(get_engine()) as session:
        job = IngestJob(document_id=1, space_id=shared["id"], status="running", stage="running"); session.add(job)
        page = WikiPageRow(path="x.md", title="x", page_type="rule", page_key="shared.x", space_id=shared["id"]); session.add(page); session.flush()
        revision = WikiPageRevision(page_id=page.id, revision=1, frontmatter_json="{}", content_md="x"); session.add(revision)
        review = WikiReviewItem(page_id=page.id, space_id=shared["id"], status="pending"); session.add(review); session.commit()
        job_id, page_id, revision_id, review_id = job.id, page.id, revision.id, review.id
    assert client.post(f"/api/ingest-jobs/{job_id}/cancel", headers=origin, params={"space_id": shared["id"]}).status_code == 403
    assert client.post(f"/api/ingest-jobs/{job_id}/retry-failed-windows", headers=origin, params={"space_id": shared["id"]}).status_code == 403
    assert client.post(f"/api/wiki/reviews/{review_id}/reject", headers=origin, params={"space_id": shared["id"]}, json={"reason": "x"}).status_code == 403
    assert client.post(f"/api/wiki/pages/{page_id}/rollback", headers=origin, params={"space_id": shared["id"]}, json={"revision_id": revision_id}).status_code == 403


def test_checkpoint_conflicts_only_include_candidates_with_real_citations():
    from app.services.task_pipeline import assemble_task_context, checkpoint_conflicts

    hits = [
        {"id": 1, "revision": 1, "page_key": "same", "title": "A", "content": "A", "assertion_summary": "允许"},
        # Context normalization removes this duplicate page key.
        {"id": 2, "revision": 1, "page_key": "same", "title": "B", "content": "B", "assertion_summary": "禁止"},
        {"id": 3, "revision": 2, "page_key": "other", "title": "C", "content": "C", "assertion_summary": "禁止"},
    ]
    context = assemble_task_context(hits, [], max_chars=1)
    groups = checkpoint_conflicts([{
        "conflict_key": "topic",
        "candidates": hits,
    }], context["wiki_hits"])

    assert {item["id"] for item in groups[0]["candidates"]} == {1, 3}
    assert {item["wiki_page_id"] for item in context["citations"]} == {1, 3}


def test_project_scope_rejects_cross_project_resources_consistently(tmp_app_data):
    from app.models.entities import (
        DataPool,
        ExampleVariant,
        ManagedPlatformCase,
        PlatformExample,
        PlatformProfile,
        PlatformRenderRun,
    )

    client = TestClient(create_app())
    owner = client.post("/api/projects", json={"name": "Owner", "slug": "owner"}).json()
    outsider = client.post("/api/projects", json={"name": "Outsider", "slug": "outsider"}).json()
    space_id = owner["default_wiki_space_id"]
    with Session(get_engine()) as session:
        document = Document(filename="scope.md", stored_path="raw/scope.md", content_type="text/markdown", sha256="f" * 64, status="ready", space_id=space_id)
        session.add(document); session.flush()
        page = WikiPageRow(path="scope.md", title="Scope", page_type="rule", page_key="scope", space_id=space_id)
        session.add(page); session.flush()
        job = IngestJob(document_id=int(document.id), space_id=space_id, status="queued")
        review = WikiReviewItem(page_id=int(page.id), space_id=space_id, status="pending")
        pool = DataPool(wiki_space_id=space_id, name="pool")
        platform = PlatformProfile(wiki_space_id=space_id, name="platform")
        session.add(job); session.add(review); session.add(pool); session.add(platform); session.flush()
        variant = ExampleVariant(platform_id=int(platform.id), name="variant")
        session.add(variant); session.flush()
        example = PlatformExample(variant_id=int(variant.id), kind="combined", content="{}", content_hash="x")
        run = PlatformRenderRun(wiki_space_id=space_id, platform_id=int(platform.id), variant_id=int(variant.id))
        case = ManagedPlatformCase(wiki_space_id=space_id, platform_id=int(platform.id), variant_id=int(variant.id), filename="case.json", kind="combined", content="{}", content_hash="x")
        session.add(example); session.add(run); session.add(case); session.commit()
        ids = {name: int(row.id) for name, row in {
            "document": document, "page": page, "job": job, "review": review,
            "pool": pool, "platform": platform, "variant": variant,
            "example": example, "run": run, "case": case,
        }.items()}

    project_query = {"project_id": outsider["id"]}
    requests = [
        ("get", f"/api/wiki-spaces/{space_id}", None),
        ("put", f"/api/wiki-spaces/{space_id}", {"name": "blocked"}),
        ("get", f"/api/documents/{ids['document']}", None),
        ("delete", f"/api/documents/{ids['document']}", None),
        ("post", f"/api/ingest-jobs/{ids['job']}/cancel", None),
        ("get", f"/api/wiki/pages/{ids['page']}", None),
        ("post", f"/api/wiki/pages/{ids['page']}/propose-update", {"assertion_summary": "blocked"}),
        ("get", f"/api/wiki/reviews/{ids['review']}", None),
        ("post", f"/api/wiki/reviews/{ids['review']}/approve", {}),
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
    assert client.post("/api/auth/setup", headers=origin, json={"username": "admin", "password": "password1234"}).status_code == 200
    with Session(get_engine()) as session:
        first = session.exec(select(Project)).first()
        first_id, first_space = int(first.id), int(first.default_wiki_space_id)
    second = client.post("/api/projects", headers=origin, json={"name": "Second", "slug": "second-auth"}).json()
    second_id, second_space = second["id"], second["default_wiki_space_id"]

    assert client.get("/api/requirements").status_code == 422
    assert client.get(f"/api/wiki-spaces/{first_space}").status_code == 422
    assert client.post("/api/requirements", headers=origin, json={"project_id": first_id, "title": "missing", "description": "missing"}).status_code == 422
    assert client.put(f"/api/projects/{first_id}", headers=origin, json={"name": "missing"}).status_code == 422

    first_query = {"project_id": first_id}
    assert client.post(
        "/api/requirements", params=first_query, headers=origin,
        json={"project_id": second_id, "title": "cross", "description": "cross"},
    ).status_code == 404
    assert client.post(
        "/api/wiki-spaces", params=first_query, headers=origin,
        json={"name": "cross", "slug": "cross-space", "scope": "project", "project_id": second_id},
    ).status_code == 404
    assert client.post(
        "/api/tasks", params=first_query, headers=origin,
        json={"title": "cross", "description": "cross", "project_id": second_id, "wiki_space_id": second_space},
    ).status_code == 404
    assert client.post(
        "/api/data-pools", params=first_query, headers=origin,
        json={"wiki_space_id": second_space, "name": "cross", "source_kind": "json", "content": "[]"},
    ).status_code == 422
    assert client.put(
        f"/api/projects/{second_id}/shared-wikis/99999", params=first_query, headers=origin,
        json={"wiki_space_id": 99999, "enabled": True, "priority": 100},
    ).status_code == 404

    created = client.post(
        "/api/requirements", params=first_query, headers=origin,
        json={"project_id": first_id, "title": "same", "description": "same"},
    )
    assert created.status_code == 200
    private = client.post(
        "/api/wiki-spaces", params=first_query, headers=origin,
        json={"name": "same", "slug": "same-space", "scope": "project"},
    )
    assert private.status_code == 201
    assert private.json()["project_id"] == first_id
    with Session(get_engine()) as session:
        assert len(session.exec(select(Project)).all()) == 2
    shared = client.post(
        "/api/wiki-spaces", params=first_query, headers=origin,
        json={"name": "shared-auth", "slug": "shared-auth", "scope": "shared", "namespace": "shared-auth"},
    ).json()
    assert client.put(
        f"/api/projects/{first_id}/shared-wikis/{shared['id']}", params=first_query, headers=origin,
        json={"wiki_space_id": shared["id"], "enabled": True, "priority": 100},
    ).status_code == 200

    with Session(get_engine()) as session:
        session.add(User(username="member2", display_name="Member", password_hash=hash_password("password1234"), role="user", is_active=True))
        session.commit()
    client.post("/api/auth/logout", headers=origin)
    assert client.post("/api/auth/login", headers=origin, json={"username": "member2", "password": "password1234"}).status_code == 200
    assert client.put(f"/api/projects/{first_id}", params=first_query, headers=origin, json={"name": "blocked"}).status_code == 403
    assert client.delete(f"/api/projects/{first_id}/shared-wikis/{shared['id']}", params=first_query, headers=origin).status_code == 403
    assert client.delete(f"/api/projects/{first_id}", params=first_query, headers=origin).status_code == 403


def test_empty_project_delete_removes_skeleton_and_bindings_but_keeps_shared(tmp_app_data):
    from pathlib import Path
    from app.models.entities import ProjectWikiBinding
    from app.services.wiki_spaces import space_root

    client = TestClient(create_app())
    project = client.post("/api/projects", json={"name": "Disposable", "slug": "disposable"}).json()
    shared = client.post("/api/wiki-spaces", json={"name": "Shared keep", "slug": "shared-keep", "scope": "shared", "namespace": "keep"}).json()
    assert client.put(f"/api/projects/{project['id']}/shared-wikis/{shared['id']}", json={"wiki_space_id": shared["id"], "enabled": True, "priority": 100}).status_code == 200
    with Session(get_engine()) as session:
        private = session.get(WikiSpace, project["default_wiki_space_id"])
        root = Path(space_root(private))
        assert root.is_dir()

    assert client.delete(f"/api/projects/{project['id']}").status_code == 204
    assert not root.exists()
    with Session(get_engine()) as session:
        assert session.get(Project, project["id"]) is None
        assert session.get(WikiSpace, project["default_wiki_space_id"]) is None
        assert session.get(WikiSpace, shared["id"]) is not None
        assert session.exec(select(ProjectWikiBinding).where(ProjectWikiBinding.project_id == project["id"])).all() == []


def test_project_delete_and_archive_safety_guards(tmp_app_data):
    from app.models.entities import Requirement
    from app.services.wiki_spaces import space_root

    client = TestClient(create_app())
    default = next(item for item in client.get("/api/projects").json() if item["slug"] == "default")
    assert client.delete(f"/api/projects/{default['id']}").status_code == 409
    assert client.put(f"/api/projects/{default['id']}", json={"status": "archived"}).status_code == 409

    busy = client.post("/api/projects", json={"name": "Busy", "slug": "busy"}).json()
    with Session(get_engine()) as session:
        session.add(Requirement(project_id=busy["id"], title="R", description="D")); session.commit()
    assert client.delete(f"/api/projects/{busy['id']}").status_code == 409

    manual = client.post("/api/projects", json={"name": "Manual", "slug": "manual"}).json()
    with Session(get_engine()) as session:
        private = session.get(WikiSpace, manual["default_wiki_space_id"])
        (space_root(private) / "manual.txt").write_text("keep", encoding="utf-8")
    assert client.delete(f"/api/projects/{manual['id']}").status_code == 409

    last = client.post("/api/projects", json={"name": "Last active", "slug": "last-active"}).json()
    with Session(get_engine()) as session:
        for row in session.exec(select(Project).where(Project.id != last["id"])).all():
            row.status = "archived"; session.add(row)
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

    shared = client.post("/api/wiki-spaces", json={"name": "Shared archive", "slug": "shared-archive", "scope": "shared", "namespace": "shared-archive"}).json()
    assert client.post(f"/api/wiki-spaces/{shared['id']}/archive").status_code == 200


def test_project_delete_failure_restores_removed_space_skeletons(tmp_app_data, monkeypatch):
    import shutil
    from app.api import projects as projects_api
    from app.services.wiki_spaces import space_root

    client = TestClient(create_app())
    project = client.post("/api/projects", json={"name": "Rollback roots", "slug": "rollback-roots"}).json()
    second = client.post("/api/wiki-spaces", json={"name": "Second root", "slug": "second-root", "scope": "project", "project_id": project["id"]}).json()
    with Session(get_engine()) as session:
        spaces = session.exec(select(WikiSpace).where(WikiSpace.project_id == project["id"]).order_by(WikiSpace.id)).all()
        roots = [space_root(space) for space in spaces]
        space_ids = [int(space.id) for space in spaces]

    real_rmtree = shutil.rmtree
    calls = 0

    def fail_second(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second root failure")
        return real_rmtree(path)

    monkeypatch.setattr(projects_api.shutil, "rmtree", fail_second)
    response = client.delete(f"/api/projects/{project['id']}")
    assert response.status_code == 500
    with Session(get_engine()) as session:
        assert session.get(Project, project["id"]) is not None
        assert all(session.get(WikiSpace, space_id) is not None for space_id in space_ids)
    for root in roots:
        assert (root / "index.md").read_text(encoding="utf-8") == "# Wiki Index\n\n"
        assert (root / "overview.md").read_text(encoding="utf-8") == "# Wiki Overview\n\n"
        assert (root / "log.md").read_text(encoding="utf-8") == "# Wiki Log\n\n"


def test_project_delete_partial_root_failure_restores_current_skeleton(tmp_app_data, monkeypatch):
    from pathlib import Path
    from app.api import projects as projects_api
    from app.services.wiki_spaces import space_root

    client = TestClient(create_app())
    project = client.post("/api/projects", json={"name": "Partial root", "slug": "partial-root"}).json()
    with Session(get_engine()) as session:
        space = session.get(WikiSpace, project["default_wiki_space_id"])
        root = space_root(space)

    def partially_remove_then_fail(path):
        target = Path(path)
        (target / "index.md").unlink()
        (target / "pages").rmdir()
        raise OSError("injected partial root failure")

    monkeypatch.setattr(projects_api.shutil, "rmtree", partially_remove_then_fail)
    response = client.delete(f"/api/projects/{project['id']}")
    assert response.status_code == 500
    with Session(get_engine()) as session:
        assert session.get(Project, project["id"]) is not None
        assert session.get(WikiSpace, project["default_wiki_space_id"]) is not None
    assert (root / "pages").is_dir()
    assert (root / "index.md").read_text(encoding="utf-8") == "# Wiki Index\n\n"
    assert (root / "overview.md").read_text(encoding="utf-8") == "# Wiki Overview\n\n"
    assert (root / "log.md").read_text(encoding="utf-8") == "# Wiki Log\n\n"
