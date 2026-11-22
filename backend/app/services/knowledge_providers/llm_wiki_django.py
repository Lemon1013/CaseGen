"""LlmWikiDjangoProvider adhering to docs/llm-wiki-reference/API_README.md."""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.models.external_wiki import ProjectKnowledgeSource
from app.services.knowledge_providers.base import (
    KnowledgeHit,
    KnowledgeProviderError,
    ProviderCapabilities,
    ProviderFieldSpec,
    ProviderTypeInfo,
    RemoteFileContent,
    RemoteFileItem,
    RemoteScope,
    parse_json_dict,
    validate_http_base_url,
    validate_safe_file_path,
)
from app.services.knowledge_providers.circuit import (
    SourceCircuitBreaker,
    knowledge_circuit_breaker,
)
from app.services.llm_http import select_environment_proxy

_RETRYABLE_STATUS_CODES = {502, 503, 504}
_RETRY_BACKOFF_SEC = 0.05


def _clamp_timeout(raw: Any) -> float:
    try:
        value = float(raw if raw is not None else 10.0)
    except (TypeError, ValueError):
        value = 10.0
    return min(120.0, max(1.0, value))


def _clamp_top_k(raw: Any, default: int = 6) -> int:
    try:
        value = int(raw if raw is not None else default)
    except (TypeError, ValueError):
        value = default
    return min(50, max(1, value))


def _safe_error_detail(resp: httpx.Response, fallback: str) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return fallback
    if isinstance(payload, dict):
        for key in ("error", "detail", "message"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:200]
    return fallback


class _SessionCache:
    """Thread-safe in-memory cookie jar cache keyed by source and credential digest."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[int, str, str, str], dict[str, str]] = {}

    def get(self, key: tuple[int, str, str, str]) -> dict[str, str] | None:
        with self._lock:
            entry = self._entries.get(key)
            return dict(entry) if entry is not None else None

    def set(self, key: tuple[int, str, str, str], cookies: dict[str, str]) -> None:
        with self._lock:
            self._entries[key] = dict(cookies)

    def delete(self, key: tuple[int, str, str, str]) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class LlmWikiDjangoProvider:
    provider_type = "llm_wiki_django"

    def __init__(
        self,
        *,
        circuit_breaker: SourceCircuitBreaker | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._circuit = circuit_breaker or knowledge_circuit_breaker
        self._default_transport = transport
        self._session_cache = _SessionCache()

    def info(self) -> ProviderTypeInfo:
        return ProviderTypeInfo(
            provider_type=self.provider_type,
            display_name="外部 LLM Wiki (Django)",
            description="对接外部 LLM Wiki 服务（支持匿名与 Django Session Cookie 登录、检索与正文读取）",
            capabilities=ProviderCapabilities(
                supports_remote_scopes=True,
                supports_file_list=True,
                supports_file_content=True,
                supports_synonyms=True,
                supported_auth_types=["none", "django_session"],
            ),
            config_fields=[
                ProviderFieldSpec(
                    key="external_project_id",
                    label="外部项目 ID",
                    field_type="text",
                    required=False,
                    placeholder="先测试连接后选择或填写外部项目 ID",
                ),
                ProviderFieldSpec(
                    key="external_project_name",
                    label="外部项目名称",
                    field_type="text",
                    required=False,
                ),
                ProviderFieldSpec(
                    key="top_k",
                    label="单源检索条数",
                    field_type="number",
                    default=6,
                ),
                ProviderFieldSpec(
                    key="timeout_sec",
                    label="请求超时 (秒)",
                    field_type="number",
                    default=10.0,
                ),
                ProviderFieldSpec(
                    key="use_synonyms",
                    label="启用同义词扩展",
                    field_type="boolean",
                    default=True,
                ),
            ],
            credential_fields_by_auth={
                "none": [],
                "django_session": [
                    ProviderFieldSpec(
                        key="username",
                        label="登录账号",
                        field_type="text",
                        required=True,
                    ),
                    ProviderFieldSpec(
                        key="password",
                        label="登录密码",
                        field_type="password",
                        required=True,
                        help_text="编辑时留空则保持已保存密码不变",
                    ),
                ],
            },
        )

    def _build_client(
        self,
        url: str,
        timeout_sec: float,
        *,
        cookies: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> httpx.Client:
        eff_transport = transport if transport is not None else self._default_transport
        if eff_transport is not None:
            return httpx.Client(
                timeout=timeout_sec,
                transport=eff_transport,
                cookies=cookies,
                trust_env=False,
                follow_redirects=False,
            )
        try:
            proxy = select_environment_proxy(url)
        except ValueError as exc:
            raise KnowledgeProviderError("invalid_config", str(exc), status_code=400) from exc
        return httpx.Client(
            timeout=timeout_sec,
            proxy=proxy,
            cookies=cookies,
            trust_env=False,
            follow_redirects=False,
        )

    @staticmethod
    def _auth_cache_key(
        source: ProjectKnowledgeSource,
        base_url: str,
        username: str,
        password: str,
    ) -> tuple[int, str, str, str]:
        digest = hashlib.sha256(f"{username}\0{password}".encode("utf-8")).hexdigest()[:16]
        return (int(source.id or 0), base_url, username, digest)

    def _login(
        self,
        *,
        base_url: str,
        timeout_sec: float,
        username: str,
        password: str,
        cb_key: tuple[int, str],
        cache_key: tuple[int, str, str, str],
        transport: httpx.BaseTransport | None = None,
    ) -> dict[str, str]:
        if not self._circuit.allow_request(cb_key):
            raise KnowledgeProviderError(
                "circuit_open",
                "外部知识库服务暂不可用（已触发熔断保护），请稍后重试",
            )

        login_url = f"{base_url}/api/auth/login/"
        payload = {"username": username, "password": password}
        resp: httpx.Response | None = None
        for attempt in range(2):
            try:
                with self._build_client(login_url, timeout_sec, transport=transport) as client:
                    resp = client.post(login_url, json=payload)
                    cookies = dict(client.cookies)
            except httpx.TimeoutException as exc:
                if attempt == 0:
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError("timeout", "外部知识库登录请求超时", status_code=504) from exc
            except httpx.HTTPError as exc:
                if attempt == 0:
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError("connection_error", "无法连接外部知识库认证服务", status_code=502) from exc

            if resp.status_code in _RETRYABLE_STATUS_CODES:
                if attempt == 0:
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError(
                    "http_error",
                    f"外部知识库认证服务暂时不可用（HTTP {resp.status_code}）",
                    status_code=resp.status_code,
                )
            break

        assert resp is not None
        if resp.status_code in {401, 403}:
            self._session_cache.delete(cache_key)
            raise KnowledgeProviderError(
                "auth_failed",
                "外部知识库登录认证失败，请检查账号和密码",
                status_code=401,
            )
        if resp.status_code >= 500:
            self._circuit.record_failure(cb_key)
            raise KnowledgeProviderError(
                "http_error",
                f"外部知识库认证接口异常（HTTP {resp.status_code}）",
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            raise KnowledgeProviderError(
                "auth_failed",
                "外部知识库登录认证失败，请检查账号和密码",
                status_code=401,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            self._circuit.record_failure(cb_key)
            raise KnowledgeProviderError("http_error", "外部知识库登录响应格式异常", status_code=502) from exc

        if not isinstance(data, dict) or not data.get("authenticated"):
            self._session_cache.delete(cache_key)
            raise KnowledgeProviderError(
                "auth_failed",
                "外部知识库登录认证失败，请检查账号和密码",
                status_code=401,
            )

        if not cookies:
            cookies = dict(resp.cookies)
        if not cookies:
            self._session_cache.delete(cache_key)
            raise KnowledgeProviderError(
                "auth_failed",
                "外部知识库未返回有效的会话 Cookie",
                status_code=401,
            )

        self._session_cache.set(cache_key, cookies)
        self._circuit.record_success(cb_key)
        return cookies

    def _request_json(
        self,
        source: ProjectKnowledgeSource,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        force_reauth: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> Any:
        base_url = validate_http_base_url(source.base_url)
        auth_type = str(source.auth_type or "none").strip().lower()
        if auth_type not in {"none", "django_session"}:
            raise KnowledgeProviderError(
                "invalid_config",
                f"不支持的认证方式: {auth_type}",
                status_code=400,
            )

        cfg = parse_json_dict(source.config_json)
        creds = parse_json_dict(source.credentials_json)
        timeout_sec = _clamp_timeout(cfg.get("timeout_sec"))
        cb_key = (int(source.id or 0), base_url)

        if not self._circuit.allow_request(cb_key):
            raise KnowledgeProviderError(
                "circuit_open",
                "外部知识库服务暂不可用（已触发熔断保护），请稍后重试",
            )

        cookies: dict[str, str] | None = None
        cache_key: tuple[int, str, str, str] | None = None
        username = ""
        password = ""
        if auth_type == "django_session":
            username = str(creds.get("username") or "").strip()
            password = str(creds.get("password") or "")
            if not username or not password:
                raise KnowledgeProviderError(
                    "invalid_config",
                    "Django Session 认证需要配置用户名和密码",
                    status_code=400,
                )
            cache_key = self._auth_cache_key(source, base_url, username, password)
            if force_reauth:
                self._session_cache.delete(cache_key)
            cookies = self._session_cache.get(cache_key)
            if not cookies:
                cookies = self._login(
                    base_url=base_url,
                    timeout_sec=timeout_sec,
                    username=username,
                    password=password,
                    cb_key=cb_key,
                    cache_key=cache_key,
                    transport=transport,
                )

        url = f"{base_url}{path}"
        reauthed = False
        transient_attempt = 0

        while True:
            if not self._circuit.allow_request(cb_key):
                raise KnowledgeProviderError(
                    "circuit_open",
                    "外部知识库服务暂不可用（已触发熔断保护），请稍后重试",
                )
            try:
                with self._build_client(url, timeout_sec, cookies=cookies, transport=transport) as client:
                    resp = client.request(method, url, params=params, json=json_body)
            except httpx.TimeoutException as exc:
                if transient_attempt < 1:
                    transient_attempt += 1
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError("timeout", "外部知识库请求超时", status_code=504) from exc
            except httpx.HTTPError as exc:
                if transient_attempt < 1:
                    transient_attempt += 1
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError("connection_error", "无法连接外部知识库服务", status_code=502) from exc

            if resp.status_code in {401, 403}:
                if auth_type == "django_session" and not reauthed and cache_key is not None:
                    reauthed = True
                    self._session_cache.delete(cache_key)
                    cookies = self._login(
                        base_url=base_url,
                        timeout_sec=timeout_sec,
                        username=username,
                        password=password,
                        cb_key=cb_key,
                        cache_key=cache_key,
                        transport=transport,
                    )
                    continue
                raise KnowledgeProviderError(
                    "auth_failed",
                    "外部知识库认证失败或会话已失效，请检查账号和密码",
                    status_code=resp.status_code,
                )

            if resp.status_code in _RETRYABLE_STATUS_CODES:
                if transient_attempt < 1:
                    transient_attempt += 1
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError(
                    "http_error",
                    f"外部知识库服务暂时不可用（HTTP {resp.status_code}）",
                    status_code=resp.status_code,
                )

            if resp.status_code >= 500:
                self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError(
                    "http_error",
                    f"外部知识库服务异常（HTTP {resp.status_code}）",
                    status_code=resp.status_code,
                )

            if resp.status_code >= 400:
                fallback = f"外部知识库请求失败（HTTP {resp.status_code}）"
                raise KnowledgeProviderError(
                    "http_error",
                    _safe_error_detail(resp, fallback),
                    status_code=resp.status_code,
                )

            try:
                data = resp.json()
            except ValueError as exc:
                self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError("http_error", "外部知识库返回了无效的 JSON 响应", status_code=502) from exc

            self._circuit.record_success(cb_key)
            return data

    def list_remote_scopes(self, source: ProjectKnowledgeSource) -> list[RemoteScope]:
        data = self._request_json(source, "GET", "/api/projects/")
        raw_items = data.get("projects") if isinstance(data, dict) else data
        if not isinstance(raw_items, list):
            return []
        scopes: list[RemoteScope] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            scope_id = str(item.get("id") or "").strip()
            if not scope_id:
                continue
            scopes.append(
                RemoteScope(
                    id=scope_id,
                    name=str(item.get("name") or scope_id),
                    description=str(item.get("description") or ""),
                    path=str(item.get("path") or ""),
                    raw=item,
                )
            )
        return scopes

    def test_connection(self, source: ProjectKnowledgeSource) -> dict[str, Any]:
        # Force a fresh auth round-trip when credentials are used so test_connection
        # actively validates the supplied username/password instead of reusing stale cookies.
        data = self._request_json(source, "GET", "/api/projects/", force_reauth=True)
        raw_items = data.get("projects") if isinstance(data, dict) else data
        scopes: list[RemoteScope] = []
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                scope_id = str(item.get("id") or "").strip()
                if not scope_id:
                    continue
                scopes.append(
                    RemoteScope(
                        id=scope_id,
                        name=str(item.get("name") or scope_id),
                        description=str(item.get("description") or ""),
                        path=str(item.get("path") or ""),
                        raw=item,
                    )
                )
        return {
            "ok": True,
            "message": f"连接成功，获取到 {len(scopes)} 个外部项目",
            "scopes": scopes,
        }

    def search(
        self,
        source: ProjectKnowledgeSource,
        query: str,
        *,
        top_k: int | None = None,
    ) -> list[KnowledgeHit]:
        cfg = parse_json_dict(source.config_json)
        external_project_id = str(cfg.get("external_project_id") or "").strip()
        if not external_project_id:
            raise KnowledgeProviderError("invalid_config", "未配置外部知识库项目 ID", status_code=400)
        clean_query = str(query or "").strip()
        if not clean_query:
            return []

        eff_top_k = _clamp_top_k(top_k if top_k is not None else cfg.get("top_k"), default=6)
        use_synonyms = bool(cfg.get("use_synonyms", True))
        encoded_project_id = quote(external_project_id, safe="")
        data = self._request_json(
            source,
            "POST",
            f"/api/projects/{encoded_project_id}/search/",
            json_body={
                "query": clean_query,
                "limit": eff_top_k,
                "useSynonyms": use_synonyms,
            },
        )
        raw_results = data.get("results") if isinstance(data, dict) else []
        if not isinstance(raw_results, list):
            return []

        hits: list[KnowledgeHit] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            chunk_id = item.get("chunkId") if item.get("chunkId") is not None else item.get("chunk_id", 0)
            file_path = str(item.get("path") or item.get("documentPath") or item.get("document_path") or "").strip()
            doc_path = str(item.get("documentPath") or item.get("document_path") or file_path).strip()
            heading_path = str(item.get("headingPath") or item.get("heading_path") or "").strip()
            title = str(item.get("title") or heading_path or file_path or "外部知识切片").strip()
            raw_page_no = item.get("pageNo") if item.get("pageNo") is not None else item.get("page_no")
            page_no: int | None = None
            if isinstance(raw_page_no, int) and not isinstance(raw_page_no, bool) and raw_page_no > 0:
                page_no = raw_page_no
            try:
                score = float(item.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            snippet = str(item.get("snippet") or "")
            evidence_snippet = str(item.get("evidenceSnippet") or item.get("evidence_snippet") or "")
            raw_highlights = item.get("highlightTerms") or item.get("highlight_terms") or []
            highlight_terms = [str(t) for t in raw_highlights] if isinstance(raw_highlights, list) else []

            hits.append(
                KnowledgeHit(
                    source_id=source.id,
                    source_name=source.name,
                    provider_type=self.provider_type,
                    chunk_id=chunk_id,
                    title=title,
                    file_path=file_path,
                    document_path=doc_path,
                    heading_path=heading_path,
                    page_no=page_no,
                    score=score,
                    weighted_score=score * float(source.weight if source.weight is not None else 1.0),
                    snippet=snippet,
                    evidence_snippet=evidence_snippet,
                    highlight_terms=highlight_terms,
                    raw=item,
                )
            )
        return hits

    def list_files(self, source: ProjectKnowledgeSource) -> list[RemoteFileItem]:
        cfg = parse_json_dict(source.config_json)
        external_project_id = str(cfg.get("external_project_id") or "").strip()
        if not external_project_id:
            raise KnowledgeProviderError("invalid_config", "未配置外部知识库项目 ID", status_code=400)
        encoded_project_id = quote(external_project_id, safe="")
        data = self._request_json(source, "GET", f"/api/projects/{encoded_project_id}/files/")
        raw_items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(raw_items, list):
            return []
        files: list[RemoteFileItem] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            item_path = str(item.get("path") or "").strip()
            if not item_path:
                continue
            item_name = str(item.get("name") or item_path.rsplit("/", 1)[-1]).strip()
            raw_size = item.get("size")
            size = int(raw_size) if isinstance(raw_size, int) and not isinstance(raw_size, bool) else None
            updated_at = str(item.get("updatedAt") or item.get("updated_at") or "").strip() or None
            files.append(
                RemoteFileItem(
                    path=item_path,
                    name=item_name,
                    size=size,
                    updated_at=updated_at,
                )
            )
        return files

    def read_file_content(
        self,
        source: ProjectKnowledgeSource,
        file_path: str,
    ) -> RemoteFileContent:
        clean_path = validate_safe_file_path(file_path)
        cfg = parse_json_dict(source.config_json)
        external_project_id = str(cfg.get("external_project_id") or "").strip()
        if not external_project_id:
            raise KnowledgeProviderError("invalid_config", "未配置外部知识库项目 ID", status_code=400)
        encoded_project_id = quote(external_project_id, safe="")
        data = self._request_json(
            source,
            "GET",
            f"/api/projects/{encoded_project_id}/file-content/",
            params={"path": clean_path},
        )
        if not isinstance(data, dict):
            raise KnowledgeProviderError("http_error", "外部知识库文件内容响应格式异常", status_code=502)
        return RemoteFileContent(
            path=str(data.get("path") or clean_path),
            content=str(data.get("content") or ""),
            source_id=source.id,
            source_name=source.name,
        )
