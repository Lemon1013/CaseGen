"""WeKnora external knowledge base provider."""

from __future__ import annotations

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
_SNIPPET_MAX_LEN = 280


def _clamp_timeout(raw: Any, default: float = 15.0) -> float:
    try:
        value = float(raw if raw is not None else default)
    except (TypeError, ValueError):
        value = default
    return min(120.0, max(1.0, value))


def _clamp_top_k(raw: Any, default: int = 6) -> int:
    try:
        value = int(raw if raw is not None else default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, 200))


def _truncate_snippet(text: str, max_len: int = _SNIPPET_MAX_LEN) -> str:
    if len(text) <= max_len:
        return text
    return f"{text[:max_len].rstrip()}..."


def _extract_error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()[:200]
        elif isinstance(err, str) and err.strip():
            return err.strip()[:200]
        for key in ("message", "detail"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:200]
    return fallback


def _safe_error_detail(resp: httpx.Response, fallback: str) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return fallback
    return _extract_error_message(payload, fallback)


def _contains_tenant_required(resp: httpx.Response) -> bool:
    try:
        payload = resp.json()
    except ValueError:
        return "TENANT_REQUIRED" in (resp.text or "")
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or "")
            msg = str(err.get("message") or "")
            if "TENANT_REQUIRED" in code or "TENANT_REQUIRED" in msg:
                return True
        for key in ("code", "error", "message", "detail"):
            val = payload.get(key)
            if isinstance(val, str) and "TENANT_REQUIRED" in val:
                return True
    return "TENANT_REQUIRED" in (resp.text or "")


def _is_safe_chunk_id(chunk_id: str) -> bool:
    if not chunk_id or "\x00" in chunk_id:
        return False
    if "/" in chunk_id or "\\" in chunk_id or ".." in chunk_id:
        return False
    return True


def _extract_chunk_id(file_path: str) -> tuple[str, str]:
    clean_path = validate_safe_file_path(file_path)
    if clean_path.startswith("chunks/"):
        chunk_id = clean_path[len("chunks/") :]
    else:
        chunk_id = clean_path
    if not _is_safe_chunk_id(chunk_id):
        raise KnowledgeProviderError(
            "invalid_config",
            "WeKnora 片段路径格式不合法，应为 chunks/<chunk_id>",
            status_code=400,
        )
    return clean_path, chunk_id


class WeKnoraProvider:
    provider_type = "weknora"

    def __init__(
        self,
        *,
        circuit_breaker: SourceCircuitBreaker | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._circuit = circuit_breaker or knowledge_circuit_breaker
        self._default_transport = transport

    def info(self) -> ProviderTypeInfo:
        return ProviderTypeInfo(
            provider_type=self.provider_type,
            display_name="WeKnora 知识库",
            description="对接腾讯 WeKnora 知识库，支持按知识库混合检索与查看检索片段原文及相邻上下文。",
            capabilities=ProviderCapabilities(
                supports_remote_scopes=True,
                supports_file_list=False,
                supports_file_content=True,
                supports_synonyms=False,
                supported_auth_types=["api_key"],
            ),
            config_fields=[
                ProviderFieldSpec(
                    key="external_project_id",
                    label="目标知识库 ID",
                    field_type="text",
                    required=True,
                    help_text="点击测试连接后可从下拉列表选择，或手动填写 WeKnora 知识库 ID",
                ),
                ProviderFieldSpec(
                    key="external_project_name",
                    label="目标知识库名称",
                    field_type="text",
                    required=False,
                ),
                ProviderFieldSpec(
                    key="top_k",
                    label="默认召回条数 (top_k)",
                    field_type="number",
                    default=6,
                    required=False,
                ),
                ProviderFieldSpec(
                    key="timeout_sec",
                    label="请求超时（秒）",
                    field_type="number",
                    default=15.0,
                    required=False,
                ),
                ProviderFieldSpec(
                    key="tenant_id",
                    label="Tenant ID（可选）",
                    field_type="text",
                    default="",
                    required=False,
                    placeholder="仅平台级 API Key 需填写，空间级 Key 请留空",
                ),
            ],
            credential_fields_by_auth={
                "api_key": [
                    ProviderFieldSpec(
                        key="api_key",
                        label="API Key",
                        field_type="password",
                        required=True,
                        placeholder="填写 WeKnora 空间级或平台级 API Key（需 retrieve 或 full_access 权限）",
                    ),
                ],
            },
        )

    def _build_client(
        self,
        url: str,
        timeout_sec: float,
        *,
        headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> httpx.Client:
        eff_transport = transport if transport is not None else self._default_transport
        if eff_transport is not None:
            return httpx.Client(
                timeout=timeout_sec,
                transport=eff_transport,
                headers=headers,
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
            headers=headers,
            trust_env=False,
            follow_redirects=False,
        )

    def _build_headers(self, source: ProjectKnowledgeSource) -> dict[str, str]:
        creds = parse_json_dict(source.credentials_json)
        api_key = str(creds.get("api_key") or creds.get("key") or "").strip()
        if not api_key:
            raise KnowledgeProviderError("invalid_config", "未配置 WeKnora API Key", status_code=400)
        headers: dict[str, str] = {"X-API-Key": api_key}
        cfg = parse_json_dict(source.config_json)
        tenant_id = str(cfg.get("tenant_id") or "").strip()
        if tenant_id:
            headers["X-Tenant-ID"] = tenant_id
        return headers

    def _request_json(
        self,
        source: ProjectKnowledgeSource,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        use_circuit_breaker: bool = True,
        max_retries: int = 1,
        transport: httpx.BaseTransport | None = None,
    ) -> Any:
        base_url = validate_http_base_url(source.base_url)
        auth_type = str(source.auth_type or "api_key").strip().lower()
        if auth_type != "api_key":
            raise KnowledgeProviderError(
                "invalid_config",
                f"WeKnora 不支持的认证方式: {auth_type}",
                status_code=400,
            )

        headers = self._build_headers(source)
        cfg = parse_json_dict(source.config_json)
        timeout_sec = _clamp_timeout(cfg.get("timeout_sec"), default=15.0)
        cb_key = (int(source.id or 0), base_url)

        if use_circuit_breaker and not self._circuit.allow_request(cb_key):
            raise KnowledgeProviderError(
                "circuit_open",
                "WeKnora 服务暂不可用（已触发熔断保护），请稍后重试",
            )

        url = f"{base_url}{path}"
        transient_attempt = 0

        while True:
            if use_circuit_breaker and not self._circuit.allow_request(cb_key):
                raise KnowledgeProviderError(
                    "circuit_open",
                    "WeKnora 服务暂不可用（已触发熔断保护），请稍后重试",
                )
            try:
                with self._build_client(url, timeout_sec, headers=headers, transport=transport) as client:
                    resp = client.request(method, url, params=params, json=json_body)
            except httpx.TimeoutException as exc:
                if transient_attempt < max_retries:
                    transient_attempt += 1
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                if use_circuit_breaker:
                    self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError("timeout", "WeKnora 请求超时", status_code=504) from exc
            except httpx.HTTPError as exc:
                if transient_attempt < max_retries:
                    transient_attempt += 1
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                if use_circuit_breaker:
                    self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError("connection_error", "无法连接 WeKnora 服务", status_code=502) from exc

            if resp.status_code in {401, 403}:
                raise KnowledgeProviderError(
                    "auth_failed",
                    "WeKnora 认证失败或权限不足，请检查 API Key 及权限配置",
                    status_code=resp.status_code,
                )

            if resp.status_code == 409 and _contains_tenant_required(resp):
                raise KnowledgeProviderError(
                    "invalid_config",
                    "WeKnora 返回 TENANT_REQUIRED：若使用平台级 API Key，请在配置中填写 Tenant ID",
                    status_code=400,
                )

            if resp.status_code == 404:
                raise KnowledgeProviderError(
                    "http_error",
                    _safe_error_detail(resp, "WeKnora 资源不存在（HTTP 404）"),
                    status_code=404,
                )

            if resp.status_code in _RETRYABLE_STATUS_CODES:
                if transient_attempt < max_retries:
                    transient_attempt += 1
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                if use_circuit_breaker:
                    self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError(
                    "http_error",
                    f"WeKnora 服务暂时不可用（HTTP {resp.status_code}）",
                    status_code=resp.status_code,
                )

            if resp.status_code >= 500:
                if use_circuit_breaker:
                    self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError(
                    "http_error",
                    _safe_error_detail(resp, f"WeKnora 服务异常（HTTP {resp.status_code}）"),
                    status_code=resp.status_code,
                )

            if resp.status_code >= 400:
                raise KnowledgeProviderError(
                    "http_error",
                    _safe_error_detail(resp, f"WeKnora 请求失败（HTTP {resp.status_code}）"),
                    status_code=resp.status_code,
                )

            try:
                data = resp.json()
            except ValueError as exc:
                if use_circuit_breaker:
                    self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError(
                    "http_error",
                    "WeKnora 返回了无效的 JSON 响应",
                    status_code=502,
                ) from exc

            if isinstance(data, dict) and data.get("success") is False:
                detail = _extract_error_message(data, "WeKnora 接口返回失败")
                raise KnowledgeProviderError("http_error", detail, status_code=resp.status_code)

            self._circuit.record_success(cb_key)
            return data

    @staticmethod
    def _parse_scopes(payload: Any) -> list[RemoteScope]:
        raw_items = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(raw_items, list):
            return []
        scopes: list[RemoteScope] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            if item.get("is_temporary"):
                continue
            scope_id = str(item.get("id") or "").strip()
            if not scope_id:
                continue
            scopes.append(
                RemoteScope(
                    id=scope_id,
                    name=str(item.get("name") or scope_id),
                    description=str(item.get("description") or ""),
                    path=str(item.get("type") or ""),
                    raw=item,
                )
            )
        return scopes

    def list_remote_scopes(self, source: ProjectKnowledgeSource) -> list[RemoteScope]:
        payload = self._request_json(source, "GET", "/api/v1/knowledge-bases")
        return self._parse_scopes(payload)

    def test_connection(self, source: ProjectKnowledgeSource) -> dict[str, Any]:
        payload = self._request_json(
            source,
            "GET",
            "/api/v1/knowledge-bases",
            use_circuit_breaker=False,
        )
        scopes = self._parse_scopes(payload)
        return {
            "ok": True,
            "message": f"连接成功，发现 {len(scopes)} 个可用知识库",
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
        kb_id = str(cfg.get("external_project_id") or "").strip()
        if not kb_id:
            raise KnowledgeProviderError("invalid_config", "未配置 WeKnora 目标知识库 ID", status_code=400)

        clean_query = str(query or "").strip()
        if not clean_query:
            return []

        limit = _clamp_top_k(top_k if top_k is not None else cfg.get("top_k"), default=6)
        payload = self._request_json(
            source,
            "POST",
            "/api/v1/knowledge-search",
            json_body={
                "query": clean_query,
                "knowledge_base_ids": [kb_id],
                "match_count": limit,
            },
        )
        raw_items = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(raw_items, list):
            return []

        weight = float(source.weight if source.weight is not None else 1.0)
        hits: list[KnowledgeHit] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            chunk_id = str(item.get("id") or "").strip()
            content = str(item.get("content") or "").strip()
            title = str(
                item.get("knowledge_title")
                or item.get("knowledge_filename")
                or chunk_id
                or "未命名片段"
            ).strip()
            document_path = str(
                item.get("knowledge_filename") or item.get("knowledge_title") or ""
            ).strip()
            file_path = f"chunks/{chunk_id}" if _is_safe_chunk_id(chunk_id) else ""
            try:
                score = float(item.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0

            hits.append(
                KnowledgeHit(
                    source_id=source.id,
                    source_name=source.name,
                    provider_type=self.provider_type,
                    chunk_id=chunk_id,
                    title=title,
                    file_path=file_path,
                    document_path=document_path,
                    score=score,
                    weighted_score=score * weight,
                    snippet=_truncate_snippet(content),
                    evidence_snippet=content,
                    raw=item,
                )
            )
        return hits

    def list_files(self, source: ProjectKnowledgeSource) -> list[RemoteFileItem]:
        raise KnowledgeProviderError(
            "unsupported",
            "WeKnora 知识库不支持文件树浏览，请使用按库检索与片段原文查看",
            status_code=400,
        )

    def _fetch_neighbor_chunk_content(
        self,
        source: ProjectKnowledgeSource,
        chunk_id: str,
    ) -> str:
        if not _is_safe_chunk_id(chunk_id):
            return ""
        encoded_id = quote(chunk_id, safe="")
        try:
            payload = self._request_json(
                source,
                "GET",
                f"/api/v1/chunks/by-id/{encoded_id}",
                use_circuit_breaker=False,
                max_retries=0,
            )
        except Exception:  # noqa: BLE001
            return ""
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return ""
        return str(data.get("content") or "").strip()

    def read_file_content(
        self,
        source: ProjectKnowledgeSource,
        file_path: str,
    ) -> RemoteFileContent:
        clean_path, chunk_id = _extract_chunk_id(file_path)
        encoded_id = quote(chunk_id, safe="")
        payload = self._request_json(
            source,
            "GET",
            f"/api/v1/chunks/by-id/{encoded_id}",
        )
        main_data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(main_data, dict):
            raise KnowledgeProviderError("http_error", "WeKnora 分块响应格式异常", status_code=502)

        main_content = str(main_data.get("content") or "").strip()
        pre_chunk_id = str(main_data.get("pre_chunk_id") or "").strip()
        next_chunk_id = str(main_data.get("next_chunk_id") or "").strip()

        pre_content = ""
        if pre_chunk_id and pre_chunk_id != chunk_id:
            pre_content = self._fetch_neighbor_chunk_content(source, pre_chunk_id)

        next_content = ""
        if next_chunk_id and next_chunk_id != chunk_id and next_chunk_id != pre_chunk_id:
            next_content = self._fetch_neighbor_chunk_content(source, next_chunk_id)

        if pre_content or next_content:
            sections: list[str] = []
            if pre_content:
                sections.append(f"> **上文片段**\n\n{pre_content}")
            sections.append(f"> **命中片段**\n\n{main_content}")
            if next_content:
                sections.append(f"> **下文片段**\n\n{next_content}")
            assembled = "\n\n---\n\n".join(sections)
        else:
            assembled = main_content

        return RemoteFileContent(
            path=clean_path,
            content=assembled,
            source_id=source.id,
            source_name=source.name,
        )
