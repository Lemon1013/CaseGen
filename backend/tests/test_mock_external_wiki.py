"""End-to-end tests for the stdlib mock external LLM Wiki service.

These tests start a real HTTP server (ThreadingHTTPServer on a random port)
and exercise the actual external_wiki_client plus the hybrid retrieval
integration against it, unlike tests/test_external_wiki.py which stubs httpx
transports in memory.
"""

from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_engine
from app.main import create_app
from app.models.external_wiki import ProjectKnowledgeSource
from app.services.knowledge_providers import (
    KnowledgeProviderError,
    LlmWikiDjangoProvider,
    search_project_knowledge,
)
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
    provider = LlmWikiDjangoProvider()
    source = ProjectKnowledgeSource(project_id=1, base_url=mock_wiki_base_url, auth_type="none")
    scopes = provider.list_remote_scopes(source)
    assert {scope.id for scope in scopes} == {"proj-sse-rules", "proj-fund-rules"}
    assert "上交所交易规则知识库" in {scope.name for scope in scopes}


def test_search_hits_relevant_chunks_sorted(mock_wiki_base_url):
    provider = LlmWikiDjangoProvider()
    source = ProjectKnowledgeSource(
        id=1,
        project_id=1,
        base_url=mock_wiki_base_url,
        auth_type="none",
        config_json=json.dumps({"external_project_id": "proj-sse-rules", "top_k": 8, "timeout_sec": 5.0}),
    )
    hits = provider.search(source, "集合竞价", top_k=8)
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
    provider = LlmWikiDjangoProvider()
    source = ProjectKnowledgeSource(
        id=2,
        project_id=1,
        base_url=mock_wiki_base_url,
        auth_type="none",
        config_json=json.dumps({"external_project_id": "proj-sse-rules", "top_k": 20, "timeout_sec": 5.0}),
    )
    all_hits = provider.search(source, "竞价", top_k=20)
    assert len(all_hits) >= 3, "竞价 should match multiple built-in chunks"
    limited = provider.search(source, "竞价", top_k=2)
    assert 0 < len(limited) <= 2
    assert limited[0].chunk_id == all_hits[0].chunk_id


def test_search_unknown_project_raises_http_error(mock_wiki_base_url):
    provider = LlmWikiDjangoProvider()
    source = ProjectKnowledgeSource(
        id=3,
        project_id=1,
        base_url=mock_wiki_base_url,
        auth_type="none",
        config_json=json.dumps({"external_project_id": "proj-does-not-exist", "top_k": 5, "timeout_sec": 5.0}),
    )
    with pytest.raises(KnowledgeProviderError) as exc_info:
        provider.search(source, "集合竞价", top_k=5)
    assert exc_info.value.category == "http_error"
    assert exc_info.value.status_code == 404


def test_search_use_synonyms_expands_matches(mock_wiki_base_url):
    provider = LlmWikiDjangoProvider()
    with_src = ProjectKnowledgeSource(
        id=4,
        project_id=1,
        base_url=mock_wiki_base_url,
        auth_type="none",
        config_json=json.dumps(
            {"external_project_id": "proj-sse-rules", "top_k": 10, "timeout_sec": 5.0, "use_synonyms": True}
        ),
    )
    without_src = ProjectKnowledgeSource(
        id=5,
        project_id=1,
        base_url=mock_wiki_base_url,
        auth_type="none",
        config_json=json.dumps(
            {"external_project_id": "proj-sse-rules", "top_k": 10, "timeout_sec": 5.0, "use_synonyms": False}
        ),
    )
    with_synonyms = provider.search(with_src, "撮合成交", top_k=10)
    without_synonyms = provider.search(without_src, "撮合成交", top_k=10)
    assert len(with_synonyms) > len(without_synonyms)
    extra_ids = {hit.chunk_id for hit in with_synonyms} - {hit.chunk_id for hit in without_synonyms}
    assert extra_ids, "synonym expansion should surface chunks without the literal query terms"


def test_project_knowledge_search_with_real_mock_server(tmp_app_data, mock_wiki_base_url):
    app_client = TestClient(create_app())
    proj_resp = app_client.post(
        "/api/projects",
        json={"name": "Mock外部库集成项目", "slug": "mock-ext"},
    ).json()
    project_id = proj_resp["id"]

    with Session(get_engine()) as session:
        src = ProjectKnowledgeSource(
            project_id=project_id,
            name="Mock上交所规则库",
            provider_type="llm_wiki_django",
            base_url=mock_wiki_base_url,
            auth_type="none",
            config_json=json.dumps(
                {
                    "external_project_id": "proj-sse-rules",
                    "external_project_name": "上交所交易规则知识库",
                    "top_k": 6,
                    "timeout_sec": 5.0,
                    "use_synonyms": True,
                }
            ),
            weight=1.5,
            priority=10,
            enabled=True,
        )
        session.add(src)
        session.commit()
        session.refresh(src)

    with Session(get_engine()) as session:
        retrieved = search_project_knowledge(session, project_id, "集合竞价", top_k=10)

    assert retrieved.configured is True
    assert retrieved.empty_reason == "none"
    assert retrieved.hits, "expected external hits from the mock server"
    assert any("集合竞价" in (hit.title or "") for hit in retrieved.hits)
    assert any(hit.highlight_terms for hit in retrieved.hits)

    direct_hits = LlmWikiDjangoProvider().search(src, "集合竞价", top_k=6)
    assert direct_hits
    assert retrieved.hits[0].chunk_id == direct_hits[0].chunk_id
    assert retrieved.hits[0].weighted_score == pytest.approx(direct_hits[0].score * 1.5)


@contextmanager
def _running_server(**kwargs):
    """Start a temporary mock server via create_server(port=0, ...) with teardown."""
    httpd = mock_server.create_server("127.0.0.1", 0, **kwargs)
    thread = threading.Thread(target=httpd.serve_forever, name="mock-external-wiki-test", daemon=True)
    thread.start()
    try:
        yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_anonymous_mode_endpoints_and_file_content():
    with _running_server(require_auth=False) as (_httpd, base_url):
        with httpx.Client(base_url=base_url, timeout=5.0, trust_env=False) as client:
            health = client.get("/health")
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}

            api_health = client.get("/api/health/")
            assert api_health.status_code == 200
            assert api_health.json() == {"portal": "ok"}

            me = client.get("/api/auth/me/")
            assert me.status_code == 200
            assert me.json() == {"authenticated": False, "isAdmin": False, "username": ""}

            projects = client.get("/api/projects/")
            assert projects.status_code == 200
            assert {p["id"] for p in projects.json()["projects"]} == {
                "proj-sse-rules",
                "proj-fund-rules",
            }

            files_resp = client.get("/api/projects/proj-sse-rules/files/")
            assert files_resp.status_code == 200
            items = files_resp.json()["items"]
            assert items == [
                {
                    "path": "raw/sources/上海证券交易所交易规则(2023修订).docx",
                    "name": "上海证券交易所交易规则(2023修订).docx",
                },
                {
                    "path": "raw/sources/上海证券交易所大宗交易实施细则.docx",
                    "name": "上海证券交易所大宗交易实施细则.docx",
                },
            ]

            search_resp = client.post(
                "/api/projects/proj-sse-rules/search/",
                json={"query": "集合竞价", "limit": 5, "useSynonyms": True},
            )
            assert search_resp.status_code == 200
            results = search_resp.json()["results"]
            assert results
            for result in results:
                assert result["path"] in mock_server.MOCK_FILES["proj-sse-rules"]
                assert isinstance(result["pageNo"], int) and result["pageNo"] > 0
                for legacy_key in (
                    "chunkId",
                    "documentPath",
                    "headingPath",
                    "title",
                    "score",
                    "snippet",
                    "evidenceSnippet",
                    "highlightTerms",
                ):
                    assert legacy_key in result

            target_path = results[0]["path"]
            content_resp = client.get(
                "/api/projects/proj-sse-rules/file-content/",
                params={"path": target_path},
            )
            assert content_resp.status_code == 200
            content_data = content_resp.json()
            assert content_data["path"] == target_path
            assert "第九十四条" in content_data["content"]

            fund_search = client.post(
                "/api/projects/proj-fund-rules/search/",
                json={"query": "巨额赎回", "limit": 3},
            )
            assert fund_search.status_code == 200
            fund_top = fund_search.json()["results"][0]
            assert fund_top["path"] == "raw/sources/公开募集证券投资基金运作管理办法.docx"
            fund_content = client.get(
                "/api/projects/proj-fund-rules/file-content/",
                params={"path": fund_top["path"]},
            )
            assert fund_content.status_code == 200
            assert "第二十三条" in fund_content.json()["content"]


def test_auth_required_mode_lifecycle():
    with _running_server(
        require_auth=True,
        username="tester",
        password="secret-password",
    ) as (_httpd, base_url):
        with httpx.Client(base_url=base_url, timeout=5.0, trust_env=False) as client:
            # Exempt endpoints stay reachable before login.
            assert client.get("/health").json() == {"status": "ok"}
            assert client.get("/api/health/").json() == {"portal": "ok"}
            assert client.get("/api/auth/me/").json() == {
                "authenticated": False,
                "isAdmin": False,
                "username": "",
            }

            # Business endpoints must reject unauthenticated calls with 401.
            sample_path = "raw/sources/上海证券交易所交易规则(2023修订).docx"
            for method, url, kwargs in (
                ("GET", "/api/projects/", {}),
                ("GET", "/api/projects/proj-sse-rules/files/", {}),
                (
                    "GET",
                    "/api/projects/proj-sse-rules/file-content/",
                    {"params": {"path": sample_path}},
                ),
                (
                    "POST",
                    "/api/projects/proj-sse-rules/search/",
                    {"json": {"query": "集合竞价"}},
                ),
            ):
                unauth_resp = client.request(method, url, **kwargs)
                assert unauth_resp.status_code == 401
                assert unauth_resp.json() == {"error": "需要登录"}

            # Wrong password fails with 401.
            bad_login = client.post(
                "/api/auth/login/",
                json={"username": "tester", "password": "wrong-password"},
            )
            assert bad_login.status_code == 401
            assert bad_login.json() == {"error": "用户名或密码错误"}

            # Correct login issues sessionid cookie and authenticates /api/auth/me/.
            good_login = client.post(
                "/api/auth/login/",
                json={"username": "tester", "password": "secret-password"},
            )
            assert good_login.status_code == 200
            assert good_login.json() == {
                "authenticated": True,
                "username": "tester",
                "isAdmin": True,
                "isSuperuser": True,
                "userId": 1,
            }
            set_cookie = good_login.headers.get("set-cookie", "")
            assert "sessionid=" in set_cookie
            assert "HttpOnly" in set_cookie
            assert "SameSite=Lax" in set_cookie
            saved_sessionid = client.cookies.get("sessionid")
            assert saved_sessionid

            me_authed = client.get("/api/auth/me/")
            assert me_authed.status_code == 200
            assert me_authed.json() == {
                "authenticated": True,
                "username": "tester",
                "isAdmin": True,
                "isSuperuser": True,
                "userId": 1,
            }

            # All business endpoints succeed with session cookie.
            assert client.get("/api/projects/").status_code == 200
            assert client.get("/api/projects/proj-sse-rules/files/").status_code == 200
            search_ok = client.post(
                "/api/projects/proj-sse-rules/search/",
                json={"query": "大宗交易", "limit": 5},
            )
            assert search_ok.status_code == 200
            block_hit = search_ok.json()["results"][0]
            assert block_hit["path"] == "raw/sources/上海证券交易所大宗交易实施细则.docx"

            content_ok = client.get(
                "/api/projects/proj-sse-rules/file-content/",
                params={"path": block_hit["path"]},
            )
            assert content_ok.status_code == 200
            assert content_ok.json()["path"] == block_hit["path"]
            assert "第五条" in content_ok.json()["content"]

            # Path traversal / missing path / missing file / unknown project checks.
            for bad_path in ("../secret", "raw\\sources\\x.docx", "/etc/passwd", ""):
                bad_resp = client.get(
                    "/api/projects/proj-sse-rules/file-content/",
                    params={"path": bad_path},
                )
                assert bad_resp.status_code == 400
                assert bad_resp.json() == {"error": "非法路径"}

            missing_param = client.get("/api/projects/proj-sse-rules/file-content/")
            assert missing_param.status_code == 400
            assert missing_param.json() == {"error": "非法路径"}

            missing_file = client.get(
                "/api/projects/proj-sse-rules/file-content/",
                params={"path": "raw/sources/does-not-exist.docx"},
            )
            assert missing_file.status_code == 404
            assert missing_file.json() == {"error": "文件不存在"}

            unknown_proj_file = client.get(
                "/api/projects/unknown-proj/file-content/",
                params={"path": sample_path},
            )
            assert unknown_proj_file.status_code == 404
            assert unknown_proj_file.json() == {"error": "project not found"}

            # Logout revokes session and clears cookie.
            logout_resp = client.post("/api/auth/logout/")
            assert logout_resp.status_code == 200
            assert logout_resp.json() == {"ok": True}

            me_after_logout = client.get("/api/auth/me/")
            assert me_after_logout.status_code == 200
            assert me_after_logout.json() == {
                "authenticated": False,
                "isAdmin": False,
                "username": "",
            }

            # Replaying the revoked sessionid cookie must return 401.
            stale_headers = {"Cookie": f"sessionid={saved_sessionid}"}
            with httpx.Client(base_url=base_url, timeout=5.0, trust_env=False) as stale_client:
                assert stale_client.get("/api/projects/", headers=stale_headers).status_code == 401
                assert (
                    stale_client.get(
                        "/api/projects/proj-sse-rules/files/",
                        headers=stale_headers,
                    ).status_code
                    == 401
                )
                assert (
                    stale_client.get(
                        "/api/projects/proj-sse-rules/file-content/",
                        params={"path": sample_path},
                        headers=stale_headers,
                    ).status_code
                    == 401
                )
                assert (
                    stale_client.post(
                        "/api/projects/proj-sse-rules/search/",
                        json={"query": "集合竞价"},
                        headers=stale_headers,
                    ).status_code
                    == 401
                )


def test_create_server_instance_isolation():
    with (
        _running_server(require_auth=True, username="user-a", password="pw-a") as (
            httpd_a,
            url_a,
        ),
        _running_server(require_auth=True, username="user-b", password="pw-b") as (
            httpd_b,
            url_b,
        ),
    ):
        with (
            httpx.Client(base_url=url_a, timeout=5.0, trust_env=False) as client_a,
            httpx.Client(base_url=url_b, timeout=5.0, trust_env=False) as client_b,
        ):
            login_a = client_a.post(
                "/api/auth/login/",
                json={"username": "user-a", "password": "pw-a"},
            )
            assert login_a.status_code == 200
            token_a = client_a.cookies.get("sessionid")
            assert token_a in httpd_a.sessions
            assert token_a not in httpd_b.sessions

            # Token issued by server A is rejected by server B.
            cross = client_b.get("/api/projects/", headers={"Cookie": f"sessionid={token_a}"})
            assert cross.status_code == 401


def test_search_topk_alias_and_precedence():
    with _running_server(require_auth=False) as (_httpd, base_url):
        with httpx.Client(base_url=base_url, timeout=5.0, trust_env=False) as client:
            topk_resp = client.post(
                "/api/projects/proj-sse-rules/search/",
                json={"query": "竞价", "topK": 2},
            )
            assert topk_resp.status_code == 200
            assert len(topk_resp.json()["results"]) == 2

            # Explicit limit takes precedence over topK.
            both_resp = client.post(
                "/api/projects/proj-sse-rules/search/",
                json={"query": "竞价", "limit": 1, "topK": 3},
            )
            assert both_resp.status_code == 200
            assert len(both_resp.json()["results"]) == 1


def test_multiple_cookie_headers_merged():
    with _running_server(require_auth=True, username="admin", password="pw") as (httpd, base_url):
        with httpx.Client(base_url=base_url, timeout=5.0, trust_env=False) as client:
            login = client.post(
                "/api/auth/login/",
                json={"username": "admin", "password": "pw"},
            )
            assert login.status_code == 200
            token = client.cookies.get("sessionid")
            assert token

        port = httpd.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
        try:
            conn.putrequest("GET", "/api/auth/me/")
            conn.putheader("Cookie", "other=value")
            conn.putheader("Cookie", f"sessionid={token}")
            conn.endheaders()
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            assert resp.status == 200
            assert body["authenticated"] is True
            assert body["username"] == "admin"
        finally:
            conn.close()


def test_invalid_and_oversized_content_length_rejected():
    with _running_server(require_auth=False) as (httpd, base_url):
        port = httpd.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
        try:
            conn.putrequest("POST", "/api/projects/proj-sse-rules/search/")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", "-5")
            conn.endheaders()
            resp = conn.getresponse()
            assert resp.status == 400
            assert json.loads(resp.read().decode("utf-8")) == {"error": "invalid JSON body"}
        finally:
            conn.close()

        oversized_payload = json.dumps({"query": "x" * (1_048_576 + 100)})
        with httpx.Client(base_url=base_url, timeout=5.0, trust_env=False) as client:
            over_resp = client.post(
                "/api/projects/proj-sse-rules/search/",
                content=oversized_payload.encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            assert over_resp.status_code == 400
            assert over_resp.json() == {"error": "invalid JSON body"}
