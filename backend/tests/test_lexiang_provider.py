"""Tests for Tencent Lexiang external knowledge base provider and API integration."""

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
    KnowledgeHit,
    KnowledgeProviderError,
    LexiangProvider,
    PROVIDER_REGISTRY,
    SourceCircuitBreaker,
    knowledge_circuit_breaker,
    list_provider_types,
    search_project_knowledge,
)
from app.services.pipeline.retrieval import _build_external_metadata


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
    source_id: int = 201,
    base_url: str = "https://lxapi.lexiangla.com",
    app_key: str = "lx-app-key-001",
    app_secret: str = "lx-super-secret-xyz",
    staff_id: str = "system-bot",
    team_id: str = "",
    scope: str = "all",
    top_k: int = 6,
    weight: float = 1.5,
) -> ProjectKnowledgeSource:
    cfg = {
        "staff_id": staff_id,
        "team_id": team_id,
        "external_project_id": scope,
        "external_project_name": "",
        "top_k": top_k,
        "timeout_sec": 20.0,
    }
    creds: dict[str, str] = {}
    if app_key:
        creds["app_key"] = app_key
    if app_secret:
        creds["app_secret"] = app_secret
    return ProjectKnowledgeSource(
        id=source_id,
        project_id=1,
        name="腾讯乐享测试源",
        provider_type="lexiang",
        base_url=base_url,
        auth_type="oauth2_client_credentials",
        config_json=json.dumps(cfg, ensure_ascii=False),
        credentials_json=json.dumps(creds, ensure_ascii=False),
        weight=weight,
        priority=10,
        enabled=True,
    )


def test_lexiang_provider_metadata_and_types_endpoint(tmp_app_data, monkeypatch):
    admin_client, _ = _setup_auth_app(monkeypatch)

    assert any(item.provider_type == "lexiang" for item in list_provider_types())

    res = admin_client.get("/api/knowledge-providers/types")
    assert res.status_code == 200, res.text
    items = res.json()
    lx = next(item for item in items if item["provider_type"] == "lexiang")
    assert lx["display_name"] == "腾讯乐享知识库"
    assert lx["capabilities"] == {
        "supports_remote_scopes": True,
        "supports_file_list": False,
        "supports_file_content": False,
        "supports_synonyms": False,
        "supported_auth_types": ["oauth2_client_credentials"],
    }
    cred_keys = [f["key"] for f in lx["credential_fields_by_auth"]["oauth2_client_credentials"]]
    assert cred_keys == ["app_key", "app_secret"]
    cfg_keys = [f["key"] for f in lx["config_fields"]]
    assert cfg_keys == [
        "staff_id",
        "team_id",
        "external_project_id",
        "external_project_name",
        "top_k",
        "timeout_sec",
    ]
    cfg_by_key = {f["key"]: f for f in lx["config_fields"]}
    assert cfg_by_key["team_id"]["label"] == "团队 ID / 知识库链接（可选）"
    assert cfg_by_key["team_id"]["placeholder"] == "直接粘贴乐享知识库页面链接，或填写团队 ID / 知识库 ID"
    assert (
        cfg_by_key["external_project_id"]["help_text"]
        == "先点击「测试连接并拉取远端知识库」，再直接在此下拉选择目标知识库"
    )


def test_lexiang_token_cache_refresh_401_retry_and_secret_masking():
    now = [1000.0]
    token_calls = 0
    search_calls = 0
    current_valid_token = ["tok-1"]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, search_calls
        if request.method == "POST" and request.url.path == "/cgi-bin/token":
            token_calls += 1
            body = json.loads(request.content.decode("utf-8"))
            assert body["grant_type"] == "client_credentials"
            assert body["app_key"] == "lx-app-key-001"
            assert body["app_secret"] == "lx-super-secret-xyz"
            tok = f"tok-{token_calls}"
            current_valid_token[0] = tok
            return httpx.Response(200, json={"access_token": tok, "expires_in": 7200})

        if request.method == "POST" and request.url.path == "/cgi-bin/v1/ai/search":
            search_calls += 1
            auth = request.headers.get("authorization")
            # Simulate revoked token on the 3rd search call
            if search_calls == 3 and auth == f"Bearer {current_valid_token[0]}":
                return httpx.Response(401, json={"message": "token expired"})
            assert auth == f"Bearer {current_valid_token[0]}"
            return httpx.Response(200, json={"code": 0, "data": {"list": []}})

        return httpx.Response(404)

    provider = LexiangProvider(
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
    )
    source = _make_source()

    # 1. First search fetches token; second search hits cache
    assert provider.search(source, "测试查询1") == []
    assert provider.search(source, "测试查询2") == []
    assert token_calls == 1
    assert search_calls == 2

    # 2. 401 on business endpoint clears cache, refreshes token, and retries once
    assert provider.search(source, "测试查询3") == []
    assert token_calls == 2
    assert search_calls == 4

    # 3. Advance clock past expires_in - 300 -> automatic token refresh
    now[0] += 7000.0
    assert provider.search(source, "测试查询4") == []
    assert token_calls == 3

    # 4. 400/401 on token endpoint -> auth_failed and must not leak app_secret
    for status_code in (400, 401):
        bad_provider = LexiangProvider(
            transport=httpx.MockTransport(
                lambda req, code=status_code: httpx.Response(
                    code,
                    json={"message": "invalid app_secret: lx-super-secret-xyz"},
                )
            )
        )
        with pytest.raises(KnowledgeProviderError) as exc_info:
            bad_provider.list_remote_scopes(source)
        assert exc_info.value.category == "auth_failed"
        assert exc_info.value.status_code == status_code
        assert "lx-super-secret-xyz" not in exc_info.value.message

    # 5. 429 on token endpoint -> http_error(429)
    rate_provider = LexiangProvider(
        transport=httpx.MockTransport(lambda req: httpx.Response(429, json={"message": "too many requests"}))
    )
    with pytest.raises(KnowledgeProviderError) as exc_429:
        rate_provider.list_remote_scopes(source)
    assert exc_429.value.category == "http_error"
    assert exc_429.value.status_code == 429


def test_lexiang_scopes_and_test_connection():
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.method == "POST" and request.url.path == "/cgi-bin/token":
            token_calls += 1
            return httpx.Response(200, json={"access_token": f"tok-{token_calls}", "expires_in": 7200})
        if request.method == "GET" and request.url.path == "/cgi-bin/v1/kb/spaces":
            assert request.headers.get("authorization") == f"Bearer tok-{token_calls}"
            assert request.url.params.get("team_id") == "team-qa"
            assert request.url.params.get("limit") == "100"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "sp-001",
                            "type": "kb_space",
                            "attributes": {"name": "核心需求库"},
                        },
                        {
                            "id": "sp-002",
                            "type": "kb_space",
                            "attributes": {"name": "测试规范库"},
                        },
                    ]
                },
            )
        return httpx.Response(404)

    cb = SourceCircuitBreaker(failure_threshold=1, cooldown_sec=60.0)
    provider = LexiangProvider(circuit_breaker=cb, transport=httpx.MockTransport(handler))

    # 1. Without team_id -> returns ["all"] and guidance message
    src_no_team = _make_source(team_id="")
    scopes_no_team = provider.list_remote_scopes(src_no_team)
    assert [s.id for s in scopes_no_team] == ["all"]
    assert token_calls == 1

    # test_connection must force refresh token and bypass open circuit
    cb.record_failure((201, "https://lxapi.lexiangla.com"))
    assert cb.allow_request((201, "https://lxapi.lexiangla.com")) is False

    test_no_team = provider.test_connection(src_no_team)
    assert test_no_team["ok"] is True
    assert test_no_team["message"] == "连接成功；如需按指定知识库检索，请粘贴乐享知识库链接或填写团队 ID 后重新拉取"
    assert [s.id for s in test_no_team["scopes"]] == ["all"]
    assert token_calls == 2

    # 2. With team_id -> returns all + team:<id> + space:<id>...
    src_with_team = _make_source(team_id="team-qa")
    test_with_team = provider.test_connection(src_with_team)
    assert test_with_team["ok"] is True
    assert test_with_team["message"] == "连接成功，发现 2 个知识库"
    assert [s.id for s in test_with_team["scopes"]] == [
        "all",
        "team:team-qa",
        "space:sp-001",
        "space:sp-002",
    ]
    assert test_with_team["scopes"][2].name == "核心需求库"
    assert test_with_team["scopes"][3].name == "测试规范库"
    assert token_calls == 3


def test_lexiang_smart_scope_resolution_from_url_and_space_id():
    requests_seen: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append((request.method, request.url.path, dict(request.url.params)))
        if request.method == "POST" and request.url.path == "/cgi-bin/token":
            return httpx.Response(200, json={"access_token": "tok-smart", "expires_in": 7200})

        if request.method == "GET" and request.url.path == "/cgi-bin/v1/kb/spaces/89c388abc123":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "89c388abc123",
                        "type": "kb_space",
                        "attributes": {"name": "订单中心需求库"},
                        "relationships": {
                            "team": {
                                "data": {"id": "team-order", "type": "team"},
                            }
                        },
                    },
                    "included": [
                        {
                            "id": "team-order",
                            "type": "team",
                            "attributes": {"name": "订单研发团队"},
                        }
                    ],
                },
            )

        if request.method == "GET" and request.url.path == "/cgi-bin/v1/kb/spaces":
            team_param = request.url.params.get("team_id")
            if team_param == "team-order":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "sp-other-1",
                                "type": "kb_space",
                                "attributes": {"name": "支付接口规范库"},
                            },
                            {
                                "id": "89c388abc123",
                                "type": "kb_space",
                                "attributes": {"name": "订单中心需求库"},
                            },
                            {
                                "id": "sp-other-2",
                                "type": "kb_space",
                                "attributes": {"name": "售后业务手册"},
                            },
                        ]
                    },
                )
            return httpx.Response(404, json={"message": "team not found"})

        return httpx.Response(404)

    provider = LexiangProvider(transport=httpx.MockTransport(handler))

    # 1. Paste full Lexiang space URL -> resolves space name, team, and sibling spaces with matched space first
    url_source = _make_source(
        team_id="https://lexiangla.com/spaces/89c388abc123?company_from=corp_demo"
    )
    url_result = provider.test_connection(url_source)
    assert url_result["ok"] is True
    assert url_result["message"] == "连接成功，发现 3 个知识库"
    assert [s.id for s in url_result["scopes"]] == [
        "space:89c388abc123",
        "space:sp-other-1",
        "space:sp-other-2",
        "team:team-order",
        "all",
    ]
    assert [s.name for s in url_result["scopes"][:3]] == [
        "订单中心需求库",
        "支付接口规范库",
        "售后业务手册",
    ]
    assert "订单研发团队" in url_result["scopes"][3].name

    # 2. Paste URL with space_id query param -> resolves identically
    query_url_source = _make_source(
        team_id="https://lexiangla.com/kb/detail?space_id=89c388abc123&tab=docs"
    )
    query_scopes = provider.list_remote_scopes(query_url_source)
    assert [s.id for s in query_scopes] == [
        "space:89c388abc123",
        "space:sp-other-1",
        "space:sp-other-2",
        "team:team-order",
        "all",
    ]

    # 3. Paste bare space_id -> tries team_id list (404), then falls back to space detail lookup
    requests_seen.clear()
    bare_space_source = _make_source(team_id="89c388abc123")
    bare_result = provider.test_connection(bare_space_source)
    assert bare_result["ok"] is True
    assert bare_result["message"] == "连接成功，发现 3 个知识库"
    assert [s.id for s in bare_result["scopes"]] == [
        "space:89c388abc123",
        "space:sp-other-1",
        "space:sp-other-2",
        "team:team-order",
        "all",
    ]
    get_paths = [(path, params) for method, path, params in requests_seen if method == "GET"]
    assert get_paths == [
        ("/cgi-bin/v1/kb/spaces", {"team_id": "89c388abc123", "limit": "100"}),
        ("/cgi-bin/v1/kb/spaces/89c388abc123", {}),
        ("/cgi-bin/v1/kb/spaces", {"team_id": "team-order", "limit": "100"}),
    ]

    # 4. Paste explicit team:<id> prefix -> directly queries team spaces list
    requests_seen.clear()
    prefixed_team_source = _make_source(team_id="team:team-order")
    team_result = provider.test_connection(prefixed_team_source)
    assert team_result["ok"] is True
    assert team_result["message"] == "连接成功，发现 3 个知识库"
    assert [s.id for s in team_result["scopes"]] == [
        "all",
        "team:team-order",
        "space:sp-other-1",
        "space:89c388abc123",
        "space:sp-other-2",
    ]
    assert [(path, params) for method, path, params in requests_seen if method == "GET"] == [
        ("/cgi-bin/v1/kb/spaces", {"team_id": "team-order", "limit": "100"}),
    ]

    # 5. Paste explicit space:<id> prefix -> directly queries space detail lookup
    requests_seen.clear()
    prefixed_space_source = _make_source(team_id="space:89c388abc123")
    space_scopes = provider.list_remote_scopes(prefixed_space_source)
    assert space_scopes[0].id == "space:89c388abc123"
    assert space_scopes[0].name == "订单中心需求库"
    assert [(path, params) for method, path, params in requests_seen if method == "GET"] == [
        ("/cgi-bin/v1/kb/spaces/89c388abc123", {}),
        ("/cgi-bin/v1/kb/spaces", {"team_id": "team-order", "limit": "100"}),
    ]


def test_lexiang_search_targets_truncation_limits_and_hit_mapping():
    captured_requests: list[tuple[dict[str, str], dict[str, object]]] = []
    long_content = "# 结算规则\n\n" + ("这是腾讯乐享返回的 Markdown 命中片段正文。" * 20)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/cgi-bin/token":
            return httpx.Response(200, json={"access_token": "tok-search", "expires_in": 7200})
        if request.method == "POST" and request.url.path == "/cgi-bin/v1/ai/search":
            body = json.loads(request.content.decode("utf-8"))
            captured_requests.append((dict(request.headers), body))
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {
                                "block_id": "blk-8899",
                                "title": "支付清算接口规范",
                                "content": long_content,
                                "url": "https://lexiangla.com/docs/doc-123",
                                "score": 0.91,
                                "file_type": "pdf",
                                "file_pages": [12, 13],
                            },
                            {
                                "title": "",
                                "content": "无 block_id 与页码片段",
                                "score": 0.45,
                            },
                        ]
                    },
                },
            )
        return httpx.Response(404)

    provider = LexiangProvider(transport=httpx.MockTransport(handler))

    # Test targets mapping across all / space: / team: / kb_entry: / bare ID
    cases = [
        ("all", None),
        ("space:sp-01", [{"type": "space", "id": "sp-01"}]),
        ("team:tm-02", [{"type": "team", "id": "tm-02"}]),
        ("kb_entry:ent-03", [{"type": "kb_entry", "id": "ent-03"}]),
        ("bare-space-04", [{"type": "space", "id": "bare-space-04"}]),
    ]
    for scope_val, expected_targets in cases:
        captured_requests.clear()
        src = _make_source(scope=scope_val, staff_id="alice.wang", weight=2.0)
        long_query = "q" * 1200
        hits = provider.search(src, long_query, top_k=99)
        assert len(captured_requests) == 1
        headers, body = captured_requests[0]
        assert headers.get("x-staff-id") == "alice.wang"
        assert headers.get("authorization") == "Bearer tok-search"
        assert len(str(body["query"])) == 1024
        assert body["top_n"] == 50
        assert body["with_score"] is True
        assert body["include_fields"] == ["block_id"]
        if expected_targets is None:
            assert "targets" not in body
        else:
            assert body["targets"] == expected_targets

        assert len(hits) == 2
        first = hits[0]
        assert first.provider_type == "lexiang"
        assert first.chunk_id == "blk-8899"
        assert first.title == "支付清算接口规范"
        assert first.file_path == ""
        assert first.document_path == "https://lexiangla.com/docs/doc-123"
        assert first.page_no == 12
        assert first.score == pytest.approx(0.91)
        assert first.weighted_score == pytest.approx(1.82)
        assert first.evidence_snippet == long_content
        assert len(first.snippet) <= 283
        assert first.snippet.endswith("...")
        assert first.raw["url"] == "https://lexiangla.com/docs/doc-123"

        second = hits[1]
        assert second.title == "未命名文档"
        assert second.chunk_id == "lexiang-1"
        assert second.page_no is None

        # Verify retrieval metadata passthrough
        ext_meta = _build_external_metadata(first)
        assert ext_meta["source_url"] == "https://lexiangla.com/docs/doc-123"
        assert ext_meta["file_type"] == "pdf"
        assert ext_meta["file_pages"] == [12, 13]
        assert ext_meta["provider_type"] == "lexiang"

    # Missing staff_id -> invalid_config(400)
    with pytest.raises(KnowledgeProviderError) as exc_staff:
        provider.search(_make_source(staff_id=""), "风控")
    assert exc_staff.value.category == "invalid_config"
    assert exc_staff.value.status_code == 400

    # Non-zero business code -> http_error
    err_provider = LexiangProvider(
        transport=httpx.MockTransport(
            lambda req: (
                httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
                if req.url.path == "/cgi-bin/token"
                else httpx.Response(200, json={"code": 40001, "message": "invalid staff_id"})
            )
        )
    )
    with pytest.raises(KnowledgeProviderError) as exc_code:
        err_provider.search(_make_source(), "风控")
    assert exc_code.value.category == "http_error"
    assert "invalid staff_id" in exc_code.value.message


def test_lexiang_search_score_normalization():
    responses: list[list[dict[str, object]]] = [
        [
            {"block_id": "b1", "title": "文档1", "content": "内容1", "score": 28.0, "vector_score": 0.88},
            {"block_id": "b2", "title": "文档2", "content": "内容2", "score": 21.0, "vector_score": 0.72},
            {"block_id": "b3", "title": "文档3", "content": "内容3", "score": 14.0, "vector_score": 0.61},
            {"block_id": "b4", "title": "文档4", "content": "内容4", "score": -2.5},
            {"block_id": "b5", "title": "文档5", "content": "内容5", "score": "invalid"},
        ],
        [
            {"block_id": "u1", "title": "单位分1", "content": "内容A", "score": 0.92, "vector_score": 0.91},
            {"block_id": "u2", "title": "单位分2", "content": "内容B", "score": 0.75, "vector_score": 0.70},
            {"block_id": "u3", "title": "单位分3", "content": "内容C", "score": -0.3},
        ],
    ]
    call_idx = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_idx
        if request.method == "POST" and request.url.path == "/cgi-bin/token":
            return httpx.Response(200, json={"access_token": "tok-norm", "expires_in": 7200})
        if request.method == "POST" and request.url.path == "/cgi-bin/v1/ai/search":
            items = responses[call_idx]
            call_idx += 1
            return httpx.Response(200, json={"code": 0, "data": {"list": items}})
        return httpx.Response(404)

    provider = LexiangProvider(transport=httpx.MockTransport(handler))
    source = _make_source(weight=2.0)

    # 1. max_score > 1.0 -> normalized to [0.0, 1.0] while preserving raw scores
    high_hits = provider.search(source, "大分数归一化")
    assert [h.score for h in high_hits] == [1.0, 0.75, 0.5, 0.0, 0.0]
    assert [h.weighted_score for h in high_hits] == [2.0, 1.5, 1.0, 0.0, 0.0]
    assert [h.raw["score"] for h in high_hits] == [28.0, 21.0, 14.0, -2.5, "invalid"]
    assert high_hits[0].raw["vector_score"] == 0.88

    # 2. All scores <= 1.0 -> keep original values unchanged (negative clamped to 0.0)
    unit_hits = provider.search(source, "零到一量纲")
    assert [h.score for h in unit_hits] == [0.92, 0.75, 0.0]
    assert [h.weighted_score for h in unit_hits] == pytest.approx([1.84, 1.5, 0.0])
    assert [h.raw["score"] for h in unit_hits] == [0.92, 0.75, -0.3]
    assert unit_hits[0].raw["vector_score"] == 0.91


def test_lexiang_unsupported_file_methods():
    provider = LexiangProvider()
    source = _make_source()

    with pytest.raises(KnowledgeProviderError) as exc_list:
        provider.list_files(source)
    assert exc_list.value.category == "unsupported"
    assert exc_list.value.status_code == 400

    with pytest.raises(KnowledgeProviderError) as exc_read:
        provider.read_file_content(source, "docs/a.md")
    assert exc_read.value.category == "unsupported"
    assert exc_read.value.status_code == 400


def test_lexiang_end_to_end_via_fastapi_routes(tmp_app_data, monkeypatch):
    admin_client, project_id = _setup_auth_app(monkeypatch)

    def mock_lexiang_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/cgi-bin/token":
            body = json.loads(request.content.decode("utf-8"))
            if body.get("app_key") != "lx-e2e-key" or body.get("app_secret") != "lx-e2e-secret":
                return httpx.Response(401, json={"message": "invalid credentials"})
            return httpx.Response(200, json={"access_token": "tok-e2e", "expires_in": 7200})
        if request.headers.get("authorization") != "Bearer tok-e2e":
            return httpx.Response(401, json={"message": "unauthorized"})
        if request.method == "GET" and request.url.path == "/cgi-bin/v1/kb/spaces":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "sp-e2e", "type": "kb_space", "attributes": {"name": "端到端乐享库"}}
                    ]
                },
            )
        if request.method == "POST" and request.url.path == "/cgi-bin/v1/ai/search":
            assert request.headers.get("x-staff-id") == "qa-bot"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {
                                "block_id": "blk-e2e-1",
                                "title": "订单退款规范",
                                "content": "### 退款时效\n\n支持 7 天无理由原路退回。",
                                "url": "https://lexiangla.com/docs/refund-spec",
                                "score": 0.95,
                                "file_type": "doc",
                                "file_pages": [3],
                            }
                        ]
                    },
                },
            )
        return httpx.Response(404)

    mock_provider = LexiangProvider(transport=httpx.MockTransport(mock_lexiang_handler))
    monkeypatch.setitem(PROVIDER_REGISTRY, "lexiang", mock_provider)

    # 1. Create Lexiang knowledge source
    create_res = admin_client.post(
        f"/api/projects/{project_id}/knowledge-sources",
        headers=_origin(),
        params={"project_id": project_id},
        json={
            "name": "腾讯乐享主库",
            "provider_type": "lexiang",
            "base_url": "https://lxapi.lexiangla.com",
            "auth_type": "oauth2_client_credentials",
            "config": {
                "staff_id": "qa-bot",
                "team_id": "team-e2e",
                "external_project_id": "space:sp-e2e",
                "external_project_name": "端到端乐享库",
                "top_k": 6,
                "timeout_sec": 20.0,
            },
            "credentials": {
                "app_key": "lx-e2e-key",
                "app_secret": "lx-e2e-secret",
            },
            "weight": 1.0,
            "priority": 10,
            "enabled": True,
        },
    )
    assert create_res.status_code == 201, create_res.text
    created = create_res.json()
    source_id = int(created["id"])
    assert created["has_credentials"] is True
    assert created["auth_username"] == "lx-e2e-key"
    assert "lx-e2e-secret" not in create_res.text

    # 2. Update with blank app_secret must preserve original app_secret
    update_res = admin_client.put(
        f"/api/projects/{project_id}/knowledge-sources/{source_id}",
        headers=_origin(),
        params={"project_id": project_id},
        json={
            "name": "腾讯乐享主库-已更新",
            "credentials": {
                "app_key": "lx-e2e-key",
                "app_secret": "",
            },
        },
    )
    assert update_res.status_code == 200, update_res.text
    updated = update_res.json()
    assert updated["has_credentials"] is True
    assert updated["auth_username"] == "lx-e2e-key"
    assert "lx-e2e-secret" not in update_res.text

    # 3. Test connection reusing preserved app_secret
    test_res = admin_client.post(
        f"/api/projects/{project_id}/knowledge-sources/test",
        headers=_origin(),
        params={"project_id": project_id},
        json={
            "source_id": source_id,
            "provider_type": "lexiang",
            "base_url": "https://lxapi.lexiangla.com",
            "auth_type": "oauth2_client_credentials",
            "config": {"staff_id": "qa-bot", "team_id": "team-e2e"},
            "credentials": {"app_key": "lx-e2e-key", "app_secret": ""},
        },
    )
    assert test_res.status_code == 200, test_res.text
    test_body = test_res.json()
    assert test_body["ok"] is True
    assert [s["id"] for s in test_body["scopes"]] == ["all", "team:team-e2e", "space:sp-e2e"]

    # 4. Unified search_project_knowledge
    with Session(get_engine()) as session:
        search_res = search_project_knowledge(session, project_id, "退款时效")
    assert search_res.configured is True
    assert search_res.empty_reason == "none"
    assert len(search_res.hits) == 1
    hit = search_res.hits[0]
    assert hit.provider_type == "lexiang"
    assert hit.file_path == ""
    assert hit.document_path == "https://lexiangla.com/docs/refund-spec"
    assert hit.page_no == 3
    assert "7 天无理由原路退回" in hit.evidence_snippet

    # 5. /files and /file-content return 400 unsupported
    files_res = admin_client.get(
        f"/api/projects/{project_id}/knowledge-sources/{source_id}/files",
        params={"project_id": project_id},
    )
    assert files_res.status_code == 400

    content_res = admin_client.get(
        f"/api/projects/{project_id}/knowledge-sources/{source_id}/file-content",
        params={"project_id": project_id, "path": "docs/a.md"},
    )
    assert content_res.status_code == 400
