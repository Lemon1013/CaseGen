"""Tests for pluggable multi-provider external knowledge sources and migration 0002."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import sqlite3
import threading
from typing import Iterator

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session, select

from app import config
from app.db import get_engine, init_db, reset_engine, run_versioned_migrations
from app.main import create_app
from app.models.entities import Project, User
from app.models.external_wiki import ProjectExternalWiki, ProjectKnowledgeSource
from app.services.auth import hash_password
from app.services.knowledge_providers import (
    KnowledgeHit,
    KnowledgeProviderError,
    LlmWikiDjangoProvider,
    PROVIDER_REGISTRY,
    ProviderCapabilities,
    ProviderTypeInfo,
    SourceCircuitBreaker,
    knowledge_circuit_breaker,
    list_provider_types,
    register_provider,
    search_project_knowledge,
)
from mock_external_wiki.server import create_server


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    knowledge_circuit_breaker.reset()
    yield
    knowledge_circuit_breaker.reset()


@contextmanager
def _running_mock_wiki(
    *,
    require_auth: bool = False,
    username: str = "wiki_user",
    password: str = "wiki_secret_123",
) -> Iterator[tuple[str, object]]:
    server = create_server(
        host="127.0.0.1",
        port=0,
        require_auth=require_auth,
        username=username,
        password=password,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


def _setup_auth_app(monkeypatch) -> tuple[TestClient, int]:
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    client = TestClient(create_app())
    res = client.post(
        "/api/auth/setup",
        headers=_origin(),
        json={"username": "admin", "display_name": "Admin", "password": "CaseGen-Test1!"},
    )
    assert res.status_code == 200, res.text
    with Session(get_engine()) as session:
        project = session.exec(select(Project)).first()
        assert project is not None and project.id is not None
        project_id = int(project.id)
    return client, project_id


def _create_ordinary_user(username: str = "alice") -> TestClient:
    with Session(get_engine()) as session:
        user = User(
            username=username,
            display_name=username,
            password_hash=hash_password("CaseGen-Test1!"),
            role="user",
            is_active=True,
        )
        session.add(user)
        session.commit()
    user_client = TestClient(create_app())
    login_res = user_client.post(
        "/api/auth/login",
        headers=_origin(),
        json={"username": username, "password": "CaseGen-Test1!"},
    )
    assert login_res.status_code == 200, login_res.text
    return user_client


def test_provider_types_registry_and_endpoint(tmp_app_data, monkeypatch):
    admin_client, _ = _setup_auth_app(monkeypatch)
    user_client = _create_ordinary_user()

    assert any(item.provider_type == "llm_wiki_django" for item in list_provider_types())

    res = user_client.get("/api/knowledge-providers/types")
    assert res.status_code == 200, res.text
    items = res.json()
    assert isinstance(items, list)
    llm_wiki = next(item for item in items if item["provider_type"] == "llm_wiki_django")
    assert llm_wiki["capabilities"]["supports_file_list"] is True
    assert llm_wiki["capabilities"]["supports_file_content"] is True
    assert "django_session" in llm_wiki["capabilities"]["supported_auth_types"]
    assert "django_session" in llm_wiki["credential_fields_by_auth"]

    # Unauthenticated access is rejected when AUTH_ENABLED=True
    anon_client = TestClient(create_app())
    assert anon_client.get("/api/knowledge-providers/types").status_code == 401
    assert admin_client.get("/api/knowledge-providers/types").status_code == 200


def test_knowledge_source_crud_permissions_and_credential_masking(tmp_app_data, monkeypatch):
    admin_client, project_id = _setup_auth_app(monkeypatch)
    user_client = _create_ordinary_user()

    with _running_mock_wiki(require_auth=True, username="wiki_admin", password="TopSecretPassword!") as (base_url, _):
        # Ordinary user cannot write or test
        forbidden_create = user_client.post(
            f"/api/projects/{project_id}/knowledge-sources",
            headers=_origin(),
            params={"project_id": project_id},
            json={
                "name": "非法创建",
                "provider_type": "llm_wiki_django",
                "base_url": base_url,
                "auth_type": "django_session",
            },
        )
        assert forbidden_create.status_code == 403

        # Admin creates with credentials
        create_res = admin_client.post(
            f"/api/projects/{project_id}/knowledge-sources",
            headers=_origin(),
            params={"project_id": project_id},
            json={
                "name": "上交所法规库",
                "provider_type": "llm_wiki_django",
                "base_url": base_url,
                "auth_type": "django_session",
                "config": {
                    "external_project_id": "proj-sse-rules",
                    "external_project_name": "上交所交易规则示例库",
                    "top_k": 5,
                    "timeout_sec": 8.0,
                    "use_synonyms": True,
                },
                "credentials": {
                    "username": "wiki_admin",
                    "password": "TopSecretPassword!",
                },
                "weight": 1.5,
                "priority": 10,
                "enabled": True,
            },
        )
        assert create_res.status_code == 201, create_res.text
        created = create_res.json()
        source_id = int(created["id"])
        assert created["has_credentials"] is True
        assert created["auth_username"] == "wiki_admin"
        assert "TopSecretPassword!" not in create_res.text
        assert "password" not in created
        assert "credentials" not in created

        # Ordinary user can read masked list
        list_res = user_client.get(
            f"/api/projects/{project_id}/knowledge-sources",
            params={"project_id": project_id},
        )
        assert list_res.status_code == 200
        assert len(list_res.json()) == 1
        assert "TopSecretPassword!" not in list_res.text

        # Test connection using saved source_id without re-sending password
        test_res = admin_client.post(
            f"/api/projects/{project_id}/knowledge-sources/test",
            headers=_origin(),
            params={"project_id": project_id},
            json={
                "source_id": source_id,
                "provider_type": "llm_wiki_django",
                "base_url": base_url,
                "auth_type": "django_session",
                "credentials": {"username": "wiki_admin", "password": ""},
            },
        )
        assert test_res.status_code == 200, test_res.text
        test_data = test_res.json()
        assert test_data["ok"] is True
        assert any(scope["id"] == "proj-sse-rules" for scope in test_data["scopes"])

        # Update with empty password preserves existing password in DB
        upd_res = admin_client.put(
            f"/api/projects/{project_id}/knowledge-sources/{source_id}",
            headers=_origin(),
            params={"project_id": project_id},
            json={
                "name": "上交所法规库-更新",
                "credentials": {"username": "wiki_admin", "password": ""},
            },
        )
        assert upd_res.status_code == 200, upd_res.text
        assert upd_res.json()["has_credentials"] is True
        with Session(get_engine()) as session:
            db_row = session.get(ProjectKnowledgeSource, source_id)
            assert db_row is not None
            assert json.loads(db_row.credentials_json)["password"] == "TopSecretPassword!"

        # Switching auth_type to 'none' clears stored credentials
        clear_res = admin_client.put(
            f"/api/projects/{project_id}/knowledge-sources/{source_id}",
            headers=_origin(),
            params={"project_id": project_id},
            json={"auth_type": "none"},
        )
        assert clear_res.status_code == 200, clear_res.text
        assert clear_res.json()["has_credentials"] is False
        assert clear_res.json()["auth_username"] == ""
        with Session(get_engine()) as session:
            db_row = session.get(ProjectKnowledgeSource, source_id)
            assert db_row is not None
            assert json.loads(db_row.credentials_json) == {}

        # Ordinary user cannot update or delete
        assert (
            user_client.delete(
                f"/api/projects/{project_id}/knowledge-sources/{source_id}",
                headers=_origin(),
                params={"project_id": project_id},
            ).status_code
            == 403
        )

        # Admin delete succeeds
        del_res = admin_client.delete(
            f"/api/projects/{project_id}/knowledge-sources/{source_id}",
            headers=_origin(),
            params={"project_id": project_id},
        )
        assert del_res.status_code == 204


def test_django_session_cache_reauth_on_401_and_bad_password_error():
    login_calls = 0
    search_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_calls, search_calls
        if request.url.path == "/api/auth/login/":
            login_calls += 1
            body = json.loads(request.content.decode("utf-8"))
            if body.get("password") != "good-pass":
                return httpx.Response(401, json={"error": "用户名或密码错误"})
            return httpx.Response(
                200,
                json={"authenticated": True, "username": body["username"]},
                headers={"Set-Cookie": f"sessionid=tok-{login_calls}; Path=/; HttpOnly"},
            )
        if request.url.path == "/api/projects/p1/search/":
            search_calls += 1
            cookie = request.headers.get("cookie", "")
            # Simulate server expiring tok-1 on second search call
            if search_calls == 2 and "tok-1" in cookie:
                return httpx.Response(401, json={"error": "需要登录"})
            if f"tok-{login_calls}" in cookie:
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "chunkId": 11,
                                "path": "raw/sources/rule.docx",
                                "headingPath": "第三章",
                                "title": "交易规则",
                                "score": 9.5,
                                "snippet": "摘要",
                                "evidenceSnippet": "完整条款内容",
                            }
                        ]
                    },
                )
            return httpx.Response(401, json={"error": "需要登录"})
        return httpx.Response(404, json={"error": "not found"})

    cb = SourceCircuitBreaker(failure_threshold=3, cooldown_sec=60.0)
    provider = LlmWikiDjangoProvider(circuit_breaker=cb, transport=httpx.MockTransport(handler))

    good_source = ProjectKnowledgeSource(
        id=42,
        project_id=1,
        name="认证库",
        provider_type="llm_wiki_django",
        base_url="http://wiki.local",
        auth_type="django_session",
        config_json=json.dumps({"external_project_id": "p1", "top_k": 5}),
        credentials_json=json.dumps({"username": "u1", "password": "good-pass"}),
    )

    # First search logs in once (login_calls=1)
    hits1 = provider.search(good_source, "交易")
    assert len(hits1) == 1
    assert hits1[0].file_path == "raw/sources/rule.docx"
    assert login_calls == 1

    # Second search gets 401 on expired tok-1, auto re-logins once (login_calls=2), succeeds
    hits2 = provider.search(good_source, "交易")
    assert len(hits2) == 1
    assert login_calls == 2
    assert search_calls == 3

    # Third search reuses cached tok-2 without another login
    hits3 = provider.search(good_source, "交易")
    assert len(hits3) == 1
    assert login_calls == 2

    # Bad password fails explicitly with auth_failed and never leaks password
    bad_source = ProjectKnowledgeSource(
        id=43,
        project_id=1,
        name="错误密码库",
        provider_type="llm_wiki_django",
        base_url="http://wiki.local",
        auth_type="django_session",
        config_json=json.dumps({"external_project_id": "p1"}),
        credentials_json=json.dumps({"username": "u1", "password": "super-secret-wrong-pw"}),
    )
    with pytest.raises(KnowledgeProviderError) as exc_info:
        provider.search(bad_source, "交易")
    assert exc_info.value.category == "auth_failed"
    assert "super-secret-wrong-pw" not in exc_info.value.message
    # auth_failed must not trip circuit breaker
    assert cb.allow_request((43, "http://wiki.local")) is True


def test_multi_source_aggregation_circuit_breaker_and_empty_states(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        project = session.exec(select(Project)).first()
        assert project is not None and project.id is not None
        project_id = int(project.id)

        # 1. No sources configured -> not_configured
        unconfigured = search_project_knowledge(session, project_id, "大宗交易")
        assert unconfigured.configured is False
        assert unconfigured.empty_reason == "not_configured"
        assert "未配置知识库" in unconfigured.message

    # Register a temporary heterogeneous provider + mock healthy server
    class _StaticCustomProvider:
        provider_type = "custom_static_test"

        def info(self) -> ProviderTypeInfo:
            return ProviderTypeInfo(
                provider_type=self.provider_type,
                display_name="Custom Static",
                capabilities=ProviderCapabilities(supports_remote_scopes=False),
            )

        def test_connection(self, source):
            return {"ok": True, "message": "ok", "scopes": []}

        def list_remote_scopes(self, source):
            return []

        def search(self, source, query, *, top_k=None):
            if "不存在关键词XYZ" in query:
                return []
            return [
                KnowledgeHit(
                    chunk_id="custom-1",
                    title="自定义异构规则",
                    file_path="custom/doc.md",
                    score=10.0,
                    snippet="自定义摘录",
                    evidence_snippet="自定义完整条款",
                )
            ]

        def list_files(self, source):
            return []

        def read_file_content(self, source, file_path):
            raise KnowledgeProviderError("unsupported", "unsupported")

    register_provider(_StaticCustomProvider())
    try:
        with _running_mock_wiki(require_auth=True, username="u_agg", password="pw_agg") as (healthy_url, _):
            # Reserve an unused closed port to simulate failing source
            with _running_mock_wiki(require_auth=False) as (dead_url, _):
                pass

            with Session(get_engine()) as session:
                s_healthy = ProjectKnowledgeSource(
                    project_id=project_id,
                    name="健康外部库",
                    provider_type="llm_wiki_django",
                    base_url=healthy_url,
                    auth_type="django_session",
                    config_json=json.dumps(
                        {
                            "external_project_id": "proj-sse-rules",
                            "top_k": 5,
                            "timeout_sec": 3.0,
                        }
                    ),
                    credentials_json=json.dumps({"username": "u_agg", "password": "pw_agg"}),
                    weight=1.0,
                    priority=10,
                    enabled=True,
                )
                s_custom = ProjectKnowledgeSource(
                    project_id=project_id,
                    name="异构高权重库",
                    provider_type="custom_static_test",
                    base_url="http://custom.local",
                    auth_type="none",
                    weight=5.0,
                    priority=20,
                    enabled=True,
                )
                s_dead = ProjectKnowledgeSource(
                    project_id=project_id,
                    name="故障库",
                    provider_type="llm_wiki_django",
                    base_url=dead_url,
                    auth_type="none",
                    config_json=json.dumps({"external_project_id": "proj-sse-rules", "timeout_sec": 1.0}),
                    weight=1.0,
                    priority=30,
                    enabled=True,
                )
                session.add_all([s_healthy, s_custom, s_dead])
                session.commit()

                # Run 3 times to trip circuit breaker on s_dead while keeping healthy + custom working
                result = None
                for _ in range(3):
                    result = search_project_knowledge(session, project_id, "大宗交易", top_k=10)
                    assert result.configured is True
                    assert result.empty_reason == "none"
                    assert len(result.hits) >= 2
                    # Custom provider hit (10.0 * 5.0 = 50.0) ranks above or by weighted score
                    assert result.hits[0].weighted_score >= result.hits[-1].weighted_score
                    assert any(w.source_name == "故障库" for w in result.warnings)

                # 4th call hits open circuit immediately on s_dead
                result4 = search_project_knowledge(session, project_id, "大宗交易", top_k=10)
                dead_warning = next(w for w in result4.warnings if w.source_name == "故障库")
                assert dead_warning.category == "circuit_open"
                assert len(result4.hits) >= 2

                # 0 hits across healthy sources -> no_hits
                no_hit_res = search_project_knowledge(session, project_id, "不存在关键词XYZ")
                assert no_hit_res.configured is True
                assert no_hit_res.empty_reason == "no_hits"
                assert no_hit_res.message == "未检索到相关内容"
    finally:
        PROVIDER_REGISTRY.pop("custom_static_test", None)


def test_file_list_and_content_proxy_security(tmp_app_data, monkeypatch):
    admin_client, project_id = _setup_auth_app(monkeypatch)
    user_client = _create_ordinary_user()

    # Create a second project to verify cross-project isolation
    proj2_res = admin_client.post(
        "/api/projects",
        headers=_origin(),
        params={"project_id": project_id},
        json={"name": "其他项目", "slug": "other-proj"},
    )
    assert proj2_res.status_code == 201, proj2_res.text
    other_project_id = int(proj2_res.json()["id"])

    with _running_mock_wiki(require_auth=True, username="reader", password="reader_pw") as (base_url, _):
        src_res = admin_client.post(
            f"/api/projects/{project_id}/knowledge-sources",
            headers=_origin(),
            params={"project_id": project_id},
            json={
                "name": "文档原文库",
                "provider_type": "llm_wiki_django",
                "base_url": base_url,
                "auth_type": "django_session",
                "config": {"external_project_id": "proj-sse-rules"},
                "credentials": {"username": "reader", "password": "reader_pw"},
            },
        )
        assert src_res.status_code == 201, src_res.text
        source_id = int(src_res.json()["id"])

        # Ordinary user can list files and read file content
        files_res = user_client.get(
            f"/api/projects/{project_id}/knowledge-sources/{source_id}/files",
            params={"project_id": project_id},
        )
        assert files_res.status_code == 200, files_res.text
        files = files_res.json()
        assert len(files) >= 1
        target_path = files[0]["path"]

        content_res = user_client.get(
            f"/api/projects/{project_id}/knowledge-sources/{source_id}/file-content",
            params={"project_id": project_id, "path": target_path},
        )
        assert content_res.status_code == 200, content_res.text
        assert content_res.json()["path"] == target_path
        assert len(content_res.json()["content"]) > 20

        # Path traversal -> 400
        bad_path_res = user_client.get(
            f"/api/projects/{project_id}/knowledge-sources/{source_id}/file-content",
            params={"project_id": project_id, "path": "../secret.txt"},
        )
        assert bad_path_res.status_code == 400

        # Missing file -> 404
        missing_file_res = user_client.get(
            f"/api/projects/{project_id}/knowledge-sources/{source_id}/file-content",
            params={"project_id": project_id, "path": "raw/sources/not-exists.docx"},
        )
        assert missing_file_res.status_code == 404

        # Cross-project access -> 404
        cross_res = admin_client.get(
            f"/api/projects/{other_project_id}/knowledge-sources/{source_id}/files",
            params={"project_id": other_project_id},
        )
        assert cross_res.status_code == 404

        # Upstream bad credentials -> 502
        bad_src_res = admin_client.post(
            f"/api/projects/{project_id}/knowledge-sources",
            headers=_origin(),
            params={"project_id": project_id},
            json={
                "name": "错误凭据库",
                "provider_type": "llm_wiki_django",
                "base_url": base_url,
                "auth_type": "django_session",
                "config": {"external_project_id": "proj-sse-rules"},
                "credentials": {"username": "reader", "password": "wrong-password"},
            },
        )
        bad_source_id = int(bad_src_res.json()["id"])
        auth_fail_res = user_client.get(
            f"/api/projects/{project_id}/knowledge-sources/{bad_source_id}/file-content",
            params={"project_id": project_id, "path": target_path},
        )
        assert auth_fail_res.status_code == 502


def test_migration_0002_backfills_legacy_external_wikis_and_extends_citations(tmp_path):
    db_path = tmp_path / "legacy_pre_0002.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations ("
            "version TEXT PRIMARY KEY, name TEXT NOT NULL, applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES ('0001_v2_refactor_baseline', 'baseline')"
        )
        conn.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO projects (id, name) VALUES (7, 'Legacy Proj')")
        conn.execute(
            """
            CREATE TABLE project_external_wikis (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL UNIQUE,
                name VARCHAR NOT NULL,
                base_url VARCHAR NOT NULL,
                external_project_id VARCHAR NOT NULL,
                external_project_name VARCHAR NOT NULL,
                top_k INTEGER NOT NULL,
                timeout_sec FLOAT NOT NULL,
                weight FLOAT NOT NULL,
                use_synonyms BOOLEAN NOT NULL,
                enabled BOOLEAN NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO project_external_wikis
            (id, project_id, name, base_url, external_project_id, external_project_name,
             top_k, timeout_sec, weight, use_synonyms, enabled)
            VALUES (1, 7, '旧外部库', 'http://127.0.0.1:8091', 'legacy-ext-1', '法规库',
                    8, 4.5, 1.3, 0, 1)
            """
        )
        conn.execute(
            """
            CREATE TABLE task_citations (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                path TEXT NOT NULL
            )
            """
        )
        conn.commit()

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        applied = run_versioned_migrations(engine)
        assert applied == ["0002_project_knowledge_sources"]
        # Re-running is idempotent
        assert run_versioned_migrations(engine) == []
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        # Old table preserved
        legacy_count = conn.execute("SELECT COUNT(*) FROM project_external_wikis").fetchone()[0]
        assert legacy_count == 1

        # Migrated row in project_knowledge_sources
        rows = conn.execute(
            "SELECT project_id, name, provider_type, base_url, auth_type, config_json, weight, enabled "
            "FROM project_knowledge_sources"
        ).fetchall()
        assert len(rows) == 1
        pid, name, ptype, url, atype, cfg_raw, weight, enabled = rows[0]
        assert (pid, name, ptype, url, atype) == (
            7,
            "旧外部库",
            "llm_wiki_django",
            "http://127.0.0.1:8091",
            "none",
        )
        cfg = json.loads(cfg_raw)
        assert cfg["external_project_id"] == "legacy-ext-1"
        assert cfg["external_project_name"] == "法规库"
        assert cfg["top_k"] == 8
        assert cfg["timeout_sec"] == 4.5
        assert cfg["use_synonyms"] is False
        assert weight == 1.3
        assert bool(enabled) is True

        # task_citations extended with 4 columns
        cit_cols = {row[1] for row in conn.execute("PRAGMA table_info(task_citations)")}
        assert {
            "knowledge_source_id",
            "file_path",
            "heading_path",
            "external_metadata_json",
        }.issubset(cit_cols)


def test_init_db_migrates_legacy_external_wikis_after_create_all(tmp_app_data):
    """Regression: create_all() creates project_knowledge_sources without SQLite DEFAULTs
    before 0002 runs, so backfill must supply explicit created_at / updated_at values."""
    db_path = tmp_app_data / "meta" / "app.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY,
                name VARCHAR NOT NULL,
                slug VARCHAR NOT NULL,
                description VARCHAR NOT NULL DEFAULT '',
                status VARCHAR NOT NULL DEFAULT 'active',
                default_wiki_space_id INTEGER,
                dsh_workspace_id VARCHAR,
                workspace_dir VARCHAR NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO projects (id, name, slug) VALUES "
            "(7, 'Legacy Proj A', 'legacy-a'), "
            "(8, 'Legacy Proj B', 'legacy-b')"
        )
        conn.execute(
            """
            CREATE TABLE project_external_wikis (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL UNIQUE,
                name VARCHAR NOT NULL,
                base_url VARCHAR NOT NULL,
                external_project_id VARCHAR NOT NULL,
                external_project_name VARCHAR NOT NULL,
                top_k INTEGER NOT NULL,
                timeout_sec FLOAT NOT NULL,
                weight FLOAT NOT NULL,
                use_synonyms BOOLEAN NOT NULL,
                enabled BOOLEAN NOT NULL,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        conn.execute(
            """
            INSERT INTO project_external_wikis
            (id, project_id, name, base_url, external_project_id, external_project_name,
             top_k, timeout_sec, weight, use_synonyms, enabled, created_at, updated_at)
            VALUES
            (1, 7, '旧库A', 'http://127.0.0.1:8091', 'ext-a', '法规A', 6, 3.0, 1.0, 1, 1,
             '2026-01-02 03:04:05', '2026-01-03 04:05:06'),
            (2, 8, '旧库B', 'http://127.0.0.1:8092', 'ext-b', '法规B', 8, 5.0, 1.5, 0, 1,
             NULL, '')
            """
        )
        conn.commit()

    reset_engine()
    init_db()
    # Second call must remain idempotent
    init_db()

    with Session(get_engine()) as session:
        sources = session.exec(
            select(ProjectKnowledgeSource).order_by(ProjectKnowledgeSource.project_id)
        ).all()
        assert len(sources) == 2

        src_a, src_b = sources
        assert src_a.project_id == 7
        assert src_a.name == "旧库A"
        assert src_a.created_at == datetime(2026, 1, 2, 3, 4, 5)
        assert src_a.updated_at == datetime(2026, 1, 3, 4, 5, 6)

        assert src_b.project_id == 8
        assert src_b.name == "旧库B"
        assert isinstance(src_b.created_at, datetime)
        assert isinstance(src_b.updated_at, datetime)
        assert src_b.updated_at == src_b.created_at

    with sqlite3.connect(db_path) as conn:
        applied = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version ASC"
            ).fetchall()
        ]
        assert applied == [
            "0001_v2_refactor_baseline",
            "0002_project_knowledge_sources",
        ]
