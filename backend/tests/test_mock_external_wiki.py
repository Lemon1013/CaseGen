"""End-to-end tests for the stdlib mock external LLM Wiki service.

These tests start a real HTTP server (ThreadingHTTPServer on a random port)
and exercise the actual external_wiki_client plus the hybrid retrieval
integration against it, unlike tests/test_external_wiki.py which stubs httpx
transports in memory.
"""

from __future__ import annotations

import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_engine
from app.main import create_app
from app.models.external_wiki import ProjectExternalWiki
from app.services.external_wiki_client import (
    list_external_projects,
    search_external_knowledge,
)
from app.services.hybrid_retrieve import project_hybrid_retrieve
from mock_external_wiki import server as mock_server


@pytest.fixture()
def mock_wiki_base_url():
    """Run the mock external wiki server on a random port in a thread."""
    httpd = mock_server.build_server("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, name="mock-external-wiki", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_health_endpoint(mock_wiki_base_url):
    resp = httpx.get(f"{mock_wiki_base_url}/health", timeout=5.0)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_projects_endpoint_contract(mock_wiki_base_url):
    resp = httpx.get(f"{mock_wiki_base_url}/api/projects/", timeout=5.0)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    projects = data["projects"]
    assert len(projects) == 2
    ids = {project["id"] for project in projects}
    assert ids == {"proj-sse-rules", "proj-fund-rules"}
    for project in projects:
        assert isinstance(project["id"], str) and project["id"]
        assert isinstance(project["name"], str) and project["name"]


def test_list_external_projects_against_mock(mock_wiki_base_url):
    projects = list_external_projects(mock_wiki_base_url, timeout_sec=5.0)
    assert {project["id"] for project in projects} == {"proj-sse-rules", "proj-fund-rules"}
    assert "上交所交易规则知识库" in {project["name"] for project in projects}


def test_search_hits_relevant_chunks_sorted(mock_wiki_base_url):
    hits = search_external_knowledge(
        mock_wiki_base_url,
        "proj-sse-rules",
        "集合竞价",
        limit=8,
        timeout_sec=5.0,
    )
    assert hits, "expected non-empty search results for 集合竞价"
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(hit.highlight_terms for hit in hits)
    top = hits[0]
    assert top.title == "集合竞价成交价格的确定"
    assert "集合竞价" in top.heading_path
    assert top.score > 0
    assert all(isinstance(hit.chunk_id, int) for hit in hits)


def test_search_respects_limit(mock_wiki_base_url):
    all_hits = search_external_knowledge(
        mock_wiki_base_url,
        "proj-sse-rules",
        "竞价",
        limit=20,
        timeout_sec=5.0,
    )
    assert len(all_hits) >= 3, "竞价 should match multiple built-in chunks"
    limited = search_external_knowledge(
        mock_wiki_base_url,
        "proj-sse-rules",
        "竞价",
        limit=2,
        timeout_sec=5.0,
    )
    assert 0 < len(limited) <= 2
    assert limited[0].chunk_id == all_hits[0].chunk_id


def test_search_unknown_project_returns_empty(mock_wiki_base_url):
    hits = search_external_knowledge(
        mock_wiki_base_url,
        "proj-does-not-exist",
        "集合竞价",
        limit=5,
        timeout_sec=5.0,
    )
    assert hits == []


def test_search_use_synonyms_expands_matches(mock_wiki_base_url):
    with_synonyms = search_external_knowledge(
        mock_wiki_base_url,
        "proj-sse-rules",
        "撮合成交",
        limit=10,
        use_synonyms=True,
        timeout_sec=5.0,
    )
    without_synonyms = search_external_knowledge(
        mock_wiki_base_url,
        "proj-sse-rules",
        "撮合成交",
        limit=10,
        use_synonyms=False,
        timeout_sec=5.0,
    )
    assert len(with_synonyms) > len(without_synonyms)
    extra_ids = {hit.chunk_id for hit in with_synonyms} - {hit.chunk_id for hit in without_synonyms}
    assert extra_ids, "synonym expansion should surface chunks without the literal query terms"


def test_project_hybrid_retrieve_with_real_mock_server(tmp_app_data, mock_wiki_base_url):
    app_client = TestClient(create_app())
    proj_resp = app_client.post(
        "/api/projects",
        json={"name": "Mock外部库集成项目", "slug": "mock-ext"},
    ).json()
    project_id = proj_resp["id"]

    # Bind the project to the running mock server (enabled configuration).
    with Session(get_engine()) as session:
        session.add(
            ProjectExternalWiki(
                project_id=project_id,
                name="Mock上交所规则库",
                base_url=mock_wiki_base_url,
                external_project_id="proj-sse-rules",
                external_project_name="上交所交易规则知识库",
                top_k=6,
                timeout_sec=5.0,
                weight=1.5,
                use_synonyms=True,
                enabled=True,
            )
        )
        session.commit()

    with Session(get_engine()) as session:
        retrieved = project_hybrid_retrieve(
            session,
            "集合竞价",
            project_id=project_id,
            top_k=10,
        )

    assert retrieved["explain"]["external_wiki_enabled"] is True
    external_hits = retrieved["external_hits"]
    assert external_hits, "expected external hits from the mock server"
    for hit in external_hits:
        assert hit["citation_type"] == "external"
        assert hit["page_type"] == "external_wiki"
    assert any("集合竞价" in (hit["title"] or "") for hit in external_hits)
    assert any(hit.get("highlight_terms") for hit in external_hits)

    # Weighted score must equal the raw mock score times the configured weight.
    direct_hits = search_external_knowledge(
        mock_wiki_base_url,
        "proj-sse-rules",
        "集合竞价",
        limit=6,
        timeout_sec=5.0,
    )
    assert direct_hits
    assert external_hits[0]["id"] == direct_hits[0].chunk_id
    assert external_hits[0]["score"] == round(direct_hits[0].score * 1.5, 6)

    # External hits participate in the interleaved hit list too.
    assert any(hit.get("citation_type") == "external" for hit in retrieved["hits"])
