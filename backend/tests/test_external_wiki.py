"""Tests for legacy /external-wiki adapter, LlmWikiDjangoProvider, and context assembly."""

from __future__ import annotations

import json
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlmodel import Session

from app.db import get_engine
from app.main import create_app
from app.models.external_wiki import ProjectKnowledgeSource
from app.services.knowledge_providers import (
    KnowledgeHit,
    KnowledgeProviderError,
    LlmWikiDjangoProvider,
    search_project_knowledge,
)
from app.services.task_pipeline import assemble_task_context
from app.services.test_points import citation_label_map_from_context


def test_list_remote_scopes_success():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/"
        return httpx.Response(
            200,
            json={
                "projects": [
                    {
                        "id": "2886e610-7220-48ea-add6-f11c6d6a9fad",
                        "name": "法规库",
                        "path": "D:/sources",
                    }
                ]
            },
        )

    provider = LlmWikiDjangoProvider(transport=httpx.MockTransport(mock_handler))
    source = ProjectKnowledgeSource(project_id=1, base_url="http://127.0.0.1:8091", auth_type="none")
    scopes = provider.list_remote_scopes(source)
    assert len(scopes) == 1
    assert scopes[0].id == "2886e610-7220-48ea-add6-f11c6d6a9fad"
    assert scopes[0].name == "法规库"


def test_list_remote_scopes_failure_and_timeout():
    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal server error")

    provider = LlmWikiDjangoProvider(transport=httpx.MockTransport(error_handler))
    source = ProjectKnowledgeSource(id=901, project_id=1, base_url="http://127.0.0.1:8091", auth_type="none")
    with pytest.raises(KnowledgeProviderError) as err_info:
        provider.list_remote_scopes(source)
    assert err_info.value.category == "http_error"

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Connection timed out")

    timeout_provider = LlmWikiDjangoProvider(transport=httpx.MockTransport(timeout_handler))
    timeout_source = ProjectKnowledgeSource(id=902, project_id=1, base_url="http://127.0.0.1:8091", auth_type="none")
    with pytest.raises(KnowledgeProviderError) as timeout_info:
        timeout_provider.list_remote_scopes(timeout_source)
    assert timeout_info.value.category == "timeout"


def test_search_external_knowledge_success():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert "/api/projects/proj-123/search/" in str(request.url)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "chunkId": 183,
                        "path": "raw/sources/接口说明.docx",
                        "documentPath": "交易规则/接口说明.docx",
                        "headingPath": "竞价交易 > 订单确认消息",
                        "title": "订单确认消息",
                        "pageNo": 12,
                        "score": 12.5,
                        "snippet": "订单确认消息包含委托序号...",
                        "evidenceSnippet": "根据上海证券交易所竞价交易接口说明第三章...",
                        "highlightTerms": ["订单确认消息"],
                    }
                ]
            },
        )

    provider = LlmWikiDjangoProvider(transport=httpx.MockTransport(mock_handler))
    source = ProjectKnowledgeSource(
        id=1,
        project_id=1,
        base_url="http://127.0.0.1:8091",
        auth_type="none",
        config_json=json.dumps({"external_project_id": "proj-123", "top_k": 5}),
    )
    hits = provider.search(source, "订单确认消息", top_k=5)
    assert len(hits) == 1
    hit = hits[0]
    assert isinstance(hit, KnowledgeHit)
    assert hit.chunk_id == 183
    assert hit.file_path == "raw/sources/接口说明.docx"
    assert hit.document_path == "交易规则/接口说明.docx"
    assert hit.heading_path == "竞价交易 > 订单确认消息"
    assert hit.title == "订单确认消息"
    assert hit.page_no == 12
    assert hit.score == 12.5
    assert "订单确认消息包含" in hit.snippet
    assert "根据上海证券交易所" in hit.evidence_snippet
    assert hit.highlight_terms == ["订单确认消息"]


def test_search_external_knowledge_timeout_raises_provider_error():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Read timed out")

    provider = LlmWikiDjangoProvider(transport=httpx.MockTransport(mock_handler))
    source = ProjectKnowledgeSource(
        id=903,
        project_id=1,
        base_url="http://127.0.0.1:8091",
        auth_type="none",
        config_json=json.dumps({"external_project_id": "proj-123"}),
    )
    with pytest.raises(KnowledgeProviderError) as exc_info:
        provider.search(source, "查询")
    assert exc_info.value.category == "timeout"


def test_assemble_task_context_with_external_hits_full_budget():
    external_hits = [
        {
            "id": 183,
            "chunk_id": 183,
            "knowledge_source_id": 7,
            "source_name": "法规库",
            "provider_type": "llm_wiki_django",
            "title": "竞价交易 > 订单确认消息",
            "path": "raw/sources/接口说明.docx",
            "file_path": "raw/sources/接口说明.docx",
            "heading_path": "竞价交易 > 订单确认消息",
            "page_no": 4,
            "score": 35.0,
            "snippet": "外部简述",
            "evidence_snippet": "外部完整证据段落：收到订单确认消息后更新本地委托状态。",
            "task_citation_id": 30,
        }
    ]

    context = assemble_task_context(
        [],
        [],
        external_hits=external_hits,
        query="订单",
        max_chars=1000,
        include_explain=True,
    )

    assert context["budgets"]["wiki_chars"] == 0
    assert context["budgets"]["source_chars"] == 0
    assert context["budgets"]["external_chars"] >= 900
    assert "# 外部知识参考 (External Knowledge [E#])" in context["text"]
    assert "# Wiki 结构化知识" not in context["text"]

    ext_citation = context["citations"][0]
    assert ext_citation["label"] == "E1"
    assert ext_citation["citation_id"] == "E1"
    assert ext_citation["task_citation_id"] == 30
    assert ext_citation["knowledge_source_id"] == 7
    assert ext_citation["file_path"] == "raw/sources/接口说明.docx"
    assert ext_citation["heading_path"] == "竞价交易 > 订单确认消息"
    assert ext_citation["external_metadata"]["page_no"] == 4

    label_map = citation_label_map_from_context(context["citations"])
    assert label_map.get("e1") == 30
    assert label_map.get("[e1]") == 30


def test_project_knowledge_search_with_legacy_source_migration(tmp_app_data):
    client = TestClient(create_app())
    proj_resp = client.post("/api/projects", json={"name": "外部知识集成项目", "slug": "ext-test"}).json()
    project_id = proj_resp["id"]

    with Session(get_engine()) as session:
        src = ProjectKnowledgeSource(
            project_id=project_id,
            name="外部测试知识库",
            provider_type="llm_wiki_django",
            base_url="http://127.0.0.1:8091",
            auth_type="none",
            config_json=json.dumps(
                {
                    "external_project_id": "test-ext-id",
                    "external_project_name": "测试外部库",
                    "top_k": 5,
                }
            ),
            weight=1.5,
            enabled=True,
        )
        session.add(src)
        session.commit()

    mock_hit = KnowledgeHit(
        chunk_id=99,
        file_path="docs/api.docx",
        document_path="docs/api.docx",
        heading_path="接口规则 > 签名验证",
        title="签名验证",
        score=20.0,
        snippet="请求必须携带签名...",
        evidence_snippet="请求体必须使用 HMAC-SHA256 进行签名，密钥由平台颁发。",
        highlight_terms=["签名验证"],
    )

    with patch.object(LlmWikiDjangoProvider, "search", return_value=[mock_hit]):
        with Session(get_engine()) as session:
            retrieved = search_project_knowledge(session, project_id, "签名验证", top_k=10)

    assert retrieved.configured is True
    assert retrieved.empty_reason == "none"
    assert len(retrieved.hits) == 1
    ext_hit = retrieved.hits[0]
    assert ext_hit.weighted_score == 30.0
    assert ext_hit.heading_path == "接口规则 > 签名验证"


def test_external_wiki_api_crud_and_connection(tmp_app_data):
    client = TestClient(create_app())
    proj = client.post("/api/projects", json={"name": "API测试项目", "slug": "api-ext"}).json()
    project_id = proj["id"]

    # 1. GET default config
    get_res = client.get(f"/api/projects/{project_id}/external-wiki?project_id={project_id}")
    assert get_res.status_code == 200
    cfg = get_res.json()
    assert cfg["project_id"] == project_id
    assert cfg["base_url"] == "http://127.0.0.1:8091"
    assert cfg["enabled"] is True

    # 2. PUT update config -> stored in ProjectKnowledgeSource
    update_data = {
        "name": "公司法规知识库",
        "base_url": "http://192.168.1.100:8091",
        "external_project_id": "reg-001",
        "external_project_name": "金融法规",
        "top_k": 8,
        "timeout_sec": 5.0,
        "weight": 1.2,
        "use_synonyms": False,
        "enabled": True,
    }
    put_res = client.put(
        f"/api/projects/{project_id}/external-wiki?project_id={project_id}",
        json=update_data,
    )
    assert put_res.status_code == 200
    saved = put_res.json()
    assert saved["name"] == "公司法规知识库"
    assert saved["external_project_id"] == "reg-001"
    assert saved["weight"] == 1.2
    assert saved["use_synonyms"] is False

    # 3. GET verify updated config persisted
    get_res2 = client.get(f"/api/projects/{project_id}/external-wiki?project_id={project_id}")
    assert get_res2.status_code == 200
    assert get_res2.json()["external_project_name"] == "金融法规"

    # 4. POST test-connection delegates to LlmWikiDjangoProvider
    mock_result = {
        "ok": True,
        "message": "连接成功",
        "scopes": [{"id": "reg-001", "name": "金融法规", "path": "D:/rules"}],
    }
    with patch.object(LlmWikiDjangoProvider, "test_connection", return_value=mock_result):
        test_res = client.post(
            f"/api/projects/{project_id}/external-wiki/test-connection?project_id={project_id}",
            json={"base_url": "http://192.168.1.100:8091", "timeout_sec": 5.0},
        )
        assert test_res.status_code == 200
        data = test_res.json()
        assert data["success"] is True
        assert len(data["projects"]) == 1
        assert data["projects"][0]["id"] == "reg-001"
