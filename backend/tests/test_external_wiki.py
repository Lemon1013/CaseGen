from unittest.mock import patch
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlmodel import Session, select

from app.db import get_engine
from app.main import create_app
from app.models.entities import Project, TaskCitation
from app.models.external_wiki import ProjectExternalWiki
from app.services.external_wiki_client import (
    ExternalWikiHit,
    list_external_projects,
    search_external_knowledge,
)
from app.services.hybrid_retrieve import project_hybrid_retrieve
from app.services.task_pipeline import assemble_task_context
from app.services.test_points import citation_label_map_from_context, normalize_model_points


def test_list_external_projects_success():
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

    transport = httpx.MockTransport(mock_handler)
    projects = list_external_projects("http://127.0.0.1:8091", transport=transport)
    assert len(projects) == 1
    assert projects[0]["id"] == "2886e610-7220-48ea-add6-f11c6d6a9fad"
    assert projects[0]["name"] == "法规库"


def test_list_external_projects_failure_and_timeout():
    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal server error")

    transport = httpx.MockTransport(error_handler)
    projects = list_external_projects("http://127.0.0.1:8091", transport=transport)
    assert projects == []

    # Timeout / connection failure graceful fallback
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Connection timed out")

    timeout_transport = httpx.MockTransport(timeout_handler)
    projects_timeout = list_external_projects("http://127.0.0.1:8091", transport=timeout_transport)
    assert projects_timeout == []


def test_search_external_knowledge_success():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert "/api/projects/proj-123/search/" in str(request.url)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "chunkId": 183,
                        "documentPath": "交易规则/接口说明.docx",
                        "headingPath": "竞价交易 > 订单确认消息",
                        "title": "订单确认消息",
                        "score": 12.5,
                        "snippet": "订单确认消息包含委托序号...",
                        "evidenceSnippet": "根据上海证券交易所竞价交易接口说明第三章...",
                        "highlightTerms": ["订单确认消息"],
                    }
                ]
            },
        )

    transport = httpx.MockTransport(mock_handler)
    hits = search_external_knowledge(
        "http://127.0.0.1:8091",
        "proj-123",
        "订单确认消息",
        limit=5,
        transport=transport,
    )
    assert len(hits) == 1
    hit = hits[0]
    assert isinstance(hit, ExternalWikiHit)
    assert hit.chunk_id == 183
    assert hit.document_path == "交易规则/接口说明.docx"
    assert hit.heading_path == "竞价交易 > 订单确认消息"
    assert hit.title == "订单确认消息"
    assert hit.score == 12.5
    assert "订单确认消息包含" in hit.snippet
    assert "根据上海证券交易所" in hit.evidence_snippet
    assert hit.highlight_terms == ["订单确认消息"]


def test_search_external_knowledge_failure_fallback():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Read timed out")

    transport = httpx.MockTransport(mock_handler)
    hits = search_external_knowledge(
        "http://127.0.0.1:8091",
        "proj-123",
        "查询",
        transport=transport,
    )
    assert hits == []


def test_assemble_task_context_with_external_hits():
    wiki_hits = [
        {
            "id": 1,
            "title": "订单取消Wiki",
            "page_key": "order.cancel",
            "path": "order_cancel.md",
            "score": 50.0,
            "content": "订单取消规则说明",
            "task_citation_id": 10,
        }
    ]
    source_hits = [
        {
            "id": 2,
            "source_chunk_id": 2,
            "title": "原文块1",
            "path": "sources/spec.docx",
            "score": 40.0,
            "text": "原文条款 3.5.2 说明",
            "clause_ids": ["3.5.2"],
            "task_citation_id": 20,
        }
    ]
    external_hits = [
        {
            "id": 183,
            "title": "竞价交易 > 订单确认消息",
            "path": "交易规则/接口说明.docx",
            "heading_path": "竞价交易 > 订单确认消息",
            "score": 35.0,
            "snippet": "外部简述",
            "evidence_snippet": "外部完整证据段落：收到订单确认消息后更新本地委托状态。",
            "task_citation_id": 30,
        }
    ]

    context = assemble_task_context(
        wiki_hits,
        source_hits,
        external_hits=external_hits,
        query="订单",
        include_explain=True,
    )

    assert "external_hits" in context
    assert len(context["external_hits"]) == 1
    assert "external_context" in context
    assert "# 外部知识参考 (External Knowledge [E#])" in context["text"]

    citations = context["citations"]
    labels = [c["label"] for c in citations]
    assert "1" in labels
    assert "S1" in labels
    assert "E1" in labels

    ext_citation = next(c for c in citations if c["citation_type"] == "external")
    assert ext_citation["label"] == "E1"
    assert ext_citation["citation_id"] == "E1"
    assert ext_citation["task_citation_id"] == 30
    assert "交易规则/接口说明.docx" in ext_citation["path"]
    assert "外部完整证据段落" in ext_citation["content_excerpt"]

    # Verify citation label mapping resolves E1, [E1], e1
    label_map = citation_label_map_from_context(citations)
    assert label_map.get("e1") == 30
    assert label_map.get("[e1]") == 30


def test_project_hybrid_retrieve_with_external_wiki(tmp_app_data):
    client = TestClient(create_app())
    proj_resp = client.post("/api/projects", json={"name": "外部知识集成项目", "slug": "ext-test"}).json()
    project_id = proj_resp["id"]

    # Configure external wiki in DB
    with Session(get_engine()) as session:
        ext_cfg = ProjectExternalWiki(
            project_id=project_id,
            name="外部测试知识库",
            base_url="http://127.0.0.1:8091",
            external_project_id="test-ext-id",
            external_project_name="测试外部库",
            top_k=5,
            weight=1.5,
            enabled=True,
        )
        session.add(ext_cfg)
        session.commit()

    mock_hit = ExternalWikiHit(
        chunk_id=99,
        document_path="docs/api.docx",
        heading_path="接口规则 > 签名验证",
        title="签名验证",
        score=20.0,
        snippet="请求必须携带签名...",
        evidence_snippet="请求体必须使用 HMAC-SHA256 进行签名，密钥由平台颁发。",
        highlight_terms=["签名验证"],
    )

    with patch("app.services.hybrid_retrieve.search_external_knowledge", return_value=[mock_hit]):
        with Session(get_engine()) as session:
            retrieved = project_hybrid_retrieve(
                session,
                "签名验证",
                project_id=project_id,
                top_k=10,
            )

    assert "external_hits" in retrieved
    assert len(retrieved["external_hits"]) == 1
    ext_hit = retrieved["external_hits"][0]
    assert ext_hit["citation_type"] == "external"
    assert ext_hit["score"] == 30.0  # 20.0 * 1.5
    assert ext_hit["heading_path"] == "接口规则 > 签名验证"

    # Hits should include the external hit
    assert any(h.get("citation_type") == "external" for h in retrieved["hits"])


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

    # 2. PUT update config
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

    # 4. POST test-connection mock
    mock_projects = [{"id": "reg-001", "name": "金融法规", "path": "D:/rules"}]
    with patch("app.api.external_wikis.list_external_projects", return_value=mock_projects):
        test_res = client.post(
            f"/api/projects/{project_id}/external-wiki/test-connection?project_id={project_id}",
            json={"base_url": "http://192.168.1.100:8091", "timeout_sec": 5.0},
        )
        assert test_res.status_code == 200
        data = test_res.json()
        assert data["success"] is True
        assert len(data["projects"]) == 1
        assert data["projects"][0]["id"] == "reg-001"
