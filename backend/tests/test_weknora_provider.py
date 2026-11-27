"""Tests for WeKnora external knowledge base provider and API integration."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlmodel import Session, select

from app import config
from app.db import get_engine
from app.main import create_app
from app.models.entities import Project
from app.models.external_wiki import ProjectKnowledgeSource
from app.services.knowledge_providers import (
    KnowledgeProviderError,
    PROVIDER_REGISTRY,
    SourceCircuitBreaker,
    WeKnoraProvider,
    knowledge_circuit_breaker,
    list_provider_types,
    search_project_knowledge,
)


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    knowledge_circuit_breaker.reset()
    yield
    knowledge_circuit_breaker.reset()


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


def _make_source(
    *,
    source_id: int = 101,
    base_url: str = "http://weknora.local",
    api_key: str = "sk-weknora-secret-token",
    kb_id: str = "kb-main-001",
    tenant_id: str = "",
    top_k: int = 6,
    weight: float = 1.5,
) -> ProjectKnowledgeSource:
    cfg = {
        "external_project_id": kb_id,
        "external_project_name": "产品文档库",
        "top_k": top_k,
        "timeout_sec": 15.0,
        "tenant_id": tenant_id,
    }
    creds = {"api_key": api_key} if api_key else {}
    return ProjectKnowledgeSource(
        id=source_id,
        project_id=1,
        name="WeKnora 测试源",
        provider_type="weknora",
        base_url=base_url,
        auth_type="api_key",
        config_json=json.dumps(cfg, ensure_ascii=False),
        credentials_json=json.dumps(creds, ensure_ascii=False),
        weight=weight,
        priority=10,
        enabled=True,
    )


def test_weknora_provider_metadata_and_types_endpoint(tmp_app_data, monkeypatch):
    admin_client, _ = _setup_auth_app(monkeypatch)

    assert any(item.provider_type == "weknora" for item in list_provider_types())

    res = admin_client.get("/api/knowledge-providers/types")
    assert res.status_code == 200, res.text
    items = res.json()
    wk = next(item for item in items if item["provider_type"] == "weknora")
    assert wk["display_name"] == "WeKnora 知识库"
    assert wk["capabilities"] == {
        "supports_remote_scopes": True,
        "supports_file_list": False,
        "supports_file_content": True,
        "supports_synonyms": False,
        "supported_auth_types": ["api_key"],
    }
    cred_keys = [f["key"] for f in wk["credential_fields_by_auth"]["api_key"]]
    assert cred_keys == ["api_key"]
    cfg_keys = [f["key"] for f in wk["config_fields"]]
    assert cfg_keys == [
        "external_project_id",
        "external_project_name",
        "top_k",
        "timeout_sec",
        "tenant_id",
    ]


def test_weknora_test_connection_and_list_scopes_headers_and_temp_filter():
    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/knowledge-bases"
        seen_headers.append(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "id": "kb-1",
                        "name": "核心规范库",
                        "description": "正式文档",
                        "type": "document",
                        "is_temporary": False,
                    },
                    {
                        "id": "kb-temp",
                        "name": "临时上传库",
                        "description": "应过滤",
                        "type": "document",
                        "is_temporary": True,
                    },
                    {
                        "id": "kb-2",
                        "name": "FAQ 知识库",
                        "description": "问答",
                        "type": "faq",
                    },
                ],
            },
        )

    cb = SourceCircuitBreaker(failure_threshold=1, cooldown_sec=60.0)
    provider = WeKnoraProvider(circuit_breaker=cb, transport=httpx.MockTransport(handler))

    # Space-level API key without tenant_id
    source_no_tenant = _make_source(tenant_id="")
    scopes = provider.list_remote_scopes(source_no_tenant)
    assert [s.id for s in scopes] == ["kb-1", "kb-2"]
    assert scopes[0].name == "核心规范库"
    assert scopes[0].path == "document"
    assert scopes[1].path == "faq"
    assert seen_headers[0].get("x-api-key") == "sk-weknora-secret-token"
    assert "x-tenant-id" not in seen_headers[0]

    # Platform-level API key with tenant_id; test_connection must bypass open circuit
    source_with_tenant = _make_source(tenant_id="10086")
    cb.record_failure((101, "http://weknora.local"))
    assert cb.allow_request((101, "http://weknora.local")) is False

    test_res = provider.test_connection(source_with_tenant)
    assert test_res["ok"] is True
    assert test_res["message"] == "连接成功，发现 2 个可用知识库"
    assert [s.id for s in test_res["scopes"]] == ["kb-1", "kb-2"]
    assert seen_headers[1].get("x-api-key") == "sk-weknora-secret-token"
    assert seen_headers[1].get("x-tenant-id") == "10086"


def test_weknora_search_request_and_hit_mapping():
    captured: list[dict[str, object]] = []
    long_content = "这是一段用于测试摘要截断的长文本内容。" * 25

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/knowledge-search"
        body = json.loads(request.content.decode("utf-8"))
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "id": "chunk-abc-123",
                        "content": long_content,
                        "knowledge_title": "交易风控指南",
                        "knowledge_filename": "risk-guide.pdf",
                        "score": 0.92,
                    },
                    {
                        "id": "bad/chunk",
                        "content": "异常 ID 片段",
                        "knowledge_filename": "fallback.md",
                        "score": 0.5,
                    },
                ],
            },
        )

    provider = WeKnoraProvider(transport=httpx.MockTransport(handler))
    source = _make_source(kb_id="kb-risk-01", top_k=8, weight=2.0)

    hits = provider.search(source, "风控拦截规则", top_k=5)
    assert captured == [
        {
            "query": "风控拦截规则",
            "knowledge_base_ids": ["kb-risk-01"],
            "match_count": 5,
        }
    ]
    assert len(hits) == 2

    first = hits[0]
    assert first.chunk_id == "chunk-abc-123"
    assert first.title == "交易风控指南"
    assert first.document_path == "risk-guide.pdf"
    assert first.file_path == "chunks/chunk-abc-123"
    assert first.score == pytest.approx(0.92)
    assert first.weighted_score == pytest.approx(1.84)
    assert first.evidence_snippet == long_content
    assert len(first.snippet) <= 283
    assert first.snippet.endswith("...")

    second = hits[1]
    assert second.title == "fallback.md"
    assert second.file_path == ""

    # Missing kb_id -> invalid_config
    no_kb_source = _make_source(kb_id="")
    with pytest.raises(KnowledgeProviderError) as exc_info:
        provider.search(no_kb_source, "风控")
    assert exc_info.value.category == "invalid_config"
    assert exc_info.value.status_code == 400


def test_weknora_read_file_content_neighbors_and_degradation():
    chunks_db = {
        "c-pre": {"id": "c-pre", "content": "上文背景说明"},
        "c-main": {
            "id": "c-main",
            "content": "核心规则正文",
            "pre_chunk_id": "c-pre",
            "next_chunk_id": "c-next",
        },
        "c-next": {"id": "c-next", "content": "下文补充条款"},
        "c-solo": {"id": "c-solo", "content": "独立分块无前后文"},
        "c-degrade": {
            "id": "c-degrade",
            "content": "邻块异常时仍应返回主分块",
            "pre_chunk_id": "c-missing-404",
            "next_chunk_id": "c-timeout",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        prefix = "/api/v1/chunks/by-id/"
        assert request.url.path.startswith(prefix)
        chunk_id = request.url.path[len(prefix) :]
        if chunk_id == "c-timeout":
            raise httpx.ReadTimeout("neighbor timeout")
        item = chunks_db.get(chunk_id)
        if item is None:
            return httpx.Response(404, json={"success": False, "error": {"message": "chunk not found"}})
        return httpx.Response(200, json={"success": True, "data": item})

    cb = SourceCircuitBreaker(failure_threshold=2, cooldown_sec=60.0)
    provider = WeKnoraProvider(circuit_breaker=cb, transport=httpx.MockTransport(handler))
    source = _make_source()

    # 1. pre + main + next assembled with Markdown dividers
    res_full = provider.read_file_content(source, "chunks/c-main")
    assert res_full.path == "chunks/c-main"
    assert res_full.content == (
        "> **上文片段**\n\n上文背景说明"
        "\n\n---\n\n"
        "> **命中片段**\n\n核心规则正文"
        "\n\n---\n\n"
        "> **下文片段**\n\n下文补充条款"
    )

    # 2. Main only (also accepts bare chunk_id)
    res_solo = provider.read_file_content(source, "c-solo")
    assert res_solo.content == "独立分块无前后文"

    # 3. Neighbor 404 + timeout silently ignored, returns main chunk without tripping circuit
    res_degrade = provider.read_file_content(source, "chunks/c-degrade")
    assert res_degrade.content == "邻块异常时仍应返回主分块"
    assert cb.allow_request((101, "http://weknora.local")) is True

    # 4. Invalid paths rejected with 400
    for bad_path in ("../etc/passwd", "chunks/a/b", "chunks/", "/abs/path"):
        with pytest.raises(KnowledgeProviderError) as exc_info:
            provider.read_file_content(source, bad_path)
        assert exc_info.value.category == "invalid_config"
        assert exc_info.value.status_code == 400


def test_weknora_error_mapping_retry_and_circuit_breaker():
    # 1. 401 & 403 -> auth_failed (must not leak API key or trip circuit)
    for status_code in (401, 403):
        cb = SourceCircuitBreaker(failure_threshold=1, cooldown_sec=60.0)
        provider = WeKnoraProvider(
            circuit_breaker=cb,
            transport=httpx.MockTransport(
                lambda req, code=status_code: httpx.Response(code, json={"error": {"message": "unauthorized"}})
            ),
        )
        source = _make_source(api_key="super-secret-wk-key")
        with pytest.raises(KnowledgeProviderError) as exc_info:
            provider.list_remote_scopes(source)
        assert exc_info.value.category == "auth_failed"
        assert exc_info.value.status_code == status_code
        assert "super-secret-wk-key" not in exc_info.value.message
        assert cb.allow_request((101, "http://weknora.local")) is True

    # 2. 409 TENANT_REQUIRED -> invalid_config 400
    provider_409 = WeKnoraProvider(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                409,
                json={"success": False, "error": {"code": "TENANT_REQUIRED", "message": "tenant header required"}},
            )
        )
    )
    with pytest.raises(KnowledgeProviderError) as exc_409:
        provider_409.list_remote_scopes(_make_source())
    assert exc_409.value.category == "invalid_config"
    assert exc_409.value.status_code == 400
    assert "TENANT_REQUIRED" in exc_409.value.message

    # 3. 404 -> http_error 404
    provider_404 = WeKnoraProvider(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(404, json={"success": False, "error": {"message": "not found"}})
        )
    )
    with pytest.raises(KnowledgeProviderError) as exc_404:
        provider_404.read_file_content(_make_source(), "chunks/missing")
    assert exc_404.value.category == "http_error"
    assert exc_404.value.status_code == 404

    # 4. 503 retry once then succeed
    attempts = 0

    def retry_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"message": "temporarily unavailable"})
        return httpx.Response(200, json={"success": True, "data": []})

    provider_retry = WeKnoraProvider(transport=httpx.MockTransport(retry_handler))
    assert provider_retry.list_remote_scopes(_make_source()) == []
    assert attempts == 2

    # 5. Consecutive transient failures trip circuit breaker
    cb_trip = SourceCircuitBreaker(failure_threshold=2, cooldown_sec=60.0)
    provider_fail = WeKnoraProvider(
        circuit_breaker=cb_trip,
        transport=httpx.MockTransport(lambda req: httpx.Response(503, json={"message": "down"})),
    )
    source = _make_source()
    for _ in range(2):
        with pytest.raises(KnowledgeProviderError) as exc_503:
            provider_fail.list_remote_scopes(source)
        assert exc_503.value.category == "http_error"

    with pytest.raises(KnowledgeProviderError) as exc_open:
        provider_fail.list_remote_scopes(source)
    assert exc_open.value.category == "circuit_open"

    # 6. Missing API key -> invalid_config
    with pytest.raises(KnowledgeProviderError) as exc_no_key:
        provider_retry.list_remote_scopes(_make_source(api_key=""))
    assert exc_no_key.value.category == "invalid_config"
    assert exc_no_key.value.status_code == 400


def test_weknora_end_to_end_via_fastapi_routes(tmp_app_data, monkeypatch):
    admin_client, project_id = _setup_auth_app(monkeypatch)

    def mock_weknora_handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != "wk-e2e-key":
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})
        if request.method == "GET" and request.url.path == "/api/v1/knowledge-bases":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {"id": "kb-e2e", "name": "端到端知识库", "description": "E2E", "type": "document"},
                    ],
                },
            )
        if request.method == "POST" and request.url.path == "/api/v1/knowledge-search":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": "ck-100",
                            "content": "T+1 清算规则详细说明",
                            "knowledge_title": "清算规则手册",
                            "knowledge_filename": "clearing.pdf",
                            "score": 0.88,
                        }
                    ],
                },
            )
        if request.method == "GET" and request.url.path == "/api/v1/chunks/by-id/ck-100":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "id": "ck-100",
                        "content": "T+1 清算规则详细说明",
                        "pre_chunk_id": "ck-099",
                        "next_chunk_id": "ck-101",
                    },
                },
            )
        if request.method == "GET" and request.url.path == "/api/v1/chunks/by-id/ck-099":
            return httpx.Response(200, json={"success": True, "data": {"id": "ck-099", "content": "前置条款"}})
        if request.method == "GET" and request.url.path == "/api/v1/chunks/by-id/ck-101":
            return httpx.Response(200, json={"success": True, "data": {"id": "ck-101", "content": "后续条款"}})
        return httpx.Response(404, json={"error": {"message": "not found"}})

    mock_provider = WeKnoraProvider(transport=httpx.MockTransport(mock_weknora_handler))
    monkeypatch.setitem(PROVIDER_REGISTRY, "weknora", mock_provider)

    # 1. Create WeKnora knowledge source
    create_res = admin_client.post(
        f"/api/projects/{project_id}/knowledge-sources",
        headers=_origin(),
        params={"project_id": project_id},
        json={
            "name": "腾讯 WeKnora",
            "provider_type": "weknora",
            "base_url": "http://weknora.mock",
            "auth_type": "api_key",
            "config": {
                "external_project_id": "kb-e2e",
                "external_project_name": "端到端知识库",
                "top_k": 5,
                "timeout_sec": 10.0,
                "tenant_id": "",
            },
            "credentials": {"api_key": "wk-e2e-key"},
            "weight": 1.2,
            "priority": 10,
            "enabled": True,
        },
    )
    assert create_res.status_code == 201, create_res.text
    created = create_res.json()
    source_id = int(created["id"])
    assert created["has_credentials"] is True
    assert "wk-e2e-key" not in create_res.text

    # 2. Test connection reusing saved API key
    test_res = admin_client.post(
        f"/api/projects/{project_id}/knowledge-sources/test",
        headers=_origin(),
        params={"project_id": project_id},
        json={
            "source_id": source_id,
            "provider_type": "weknora",
            "base_url": "http://weknora.mock",
            "auth_type": "api_key",
            "config": {"tenant_id": ""},
            "credentials": {"api_key": ""},
        },
    )
    assert test_res.status_code == 200, test_res.text
    test_body = test_res.json()
    assert test_body["ok"] is True
    assert len(test_body["scopes"]) == 1
    assert test_body["scopes"][0]["id"] == "kb-e2e"

    # 3. Unified search_project_knowledge retrieves hits with chunks/<id>
    with Session(get_engine()) as session:
        search_res = search_project_knowledge(session, project_id, "清算规则")
    assert search_res.configured is True
    assert search_res.empty_reason == "none"
    assert len(search_res.hits) == 1
    hit = search_res.hits[0]
    assert hit.provider_type == "weknora"
    assert hit.file_path == "chunks/ck-100"
    assert hit.evidence_snippet == "T+1 清算规则详细说明"

    # 4. /file-content returns assembled main + pre + next chunks
    content_res = admin_client.get(
        f"/api/projects/{project_id}/knowledge-sources/{source_id}/file-content",
        params={"project_id": project_id, "path": hit.file_path},
    )
    assert content_res.status_code == 200, content_res.text
    content_body = content_res.json()
    assert content_body["path"] == "chunks/ck-100"
    assert "前置条款" in content_body["content"]
    assert "T+1 清算规则详细说明" in content_body["content"]
    assert "后续条款" in content_body["content"]

    # 5. /files returns 400 unsupported
    files_res = admin_client.get(
        f"/api/projects/{project_id}/knowledge-sources/{source_id}/files",
        params={"project_id": project_id},
    )
    assert files_res.status_code == 400
