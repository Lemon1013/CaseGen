"""Tencent Lexiang external knowledge base provider."""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlparse

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
)
from app.services.knowledge_providers.circuit import (
    SourceCircuitBreaker,
    knowledge_circuit_breaker,
)
from app.services.llm_http import select_environment_proxy

_RETRYABLE_STATUS_CODES = {502, 503, 504}
_RETRY_BACKOFF_SEC = 0.05
_SNIPPET_MAX_LEN = 280
_QUERY_MAX_LEN = 1024
_DEFAULT_TOKEN_EXPIRES_IN = 7200
_TOKEN_REFRESH_MARGIN_SEC = 300.0
_TOKEN_MIN_TTL_SEC = 60.0
_SUPPORTED_TARGET_PREFIXES = ("space", "team", "kb_entry")
_LEXIANG_SPACE_URL_RE = re.compile(r"/spaces/([A-Za-z0-9_-]+)")


def _clamp_timeout(raw: Any, default: float = 20.0) -> float:
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
    return max(1, min(value, 50))


def _truncate_snippet(text: str, max_len: int = _SNIPPET_MAX_LEN) -> str:
    if len(text) <= max_len:
        return text
    return f"{text[:max_len].rstrip()}..."


def _sanitize_secret(message: str, secret: str) -> str:
    if secret and secret in message:
        return message.replace(secret, "***")
    return message


def _extract_error_message(payload: Any, fallback: str, secret: str = "") -> str:
    if isinstance(payload, dict):
        for key in ("message", "msg", "error_description", "error", "detail"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return _sanitize_secret(val.strip()[:200], secret)
            if isinstance(val, dict):
                nested = val.get("message") or val.get("detail")
                if isinstance(nested, str) and nested.strip():
                    return _sanitize_secret(nested.strip()[:200], secret)
    return _sanitize_secret(fallback, secret)


def _safe_error_detail(resp: httpx.Response, fallback: str, secret: str = "") -> str:
    try:
        payload = resp.json()
    except ValueError:
        return _sanitize_secret(fallback, secret)
    return _extract_error_message(payload, fallback, secret=secret)


def _parse_first_page_no(raw_pages: Any) -> int | None:
    if not isinstance(raw_pages, list) or not raw_pages:
        return None
    first = raw_pages[0]
    if isinstance(first, bool):
        return None
    try:
        return int(first)
    except (TypeError, ValueError):
        return None


def _parse_raw_score(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(score) or score < 0.0:
        return 0.0
    return score


def _parse_targets(raw_scope: str) -> list[dict[str, str]] | None:
    cleaned = str(raw_scope or "").strip()
    if not cleaned or cleaned.lower() == "all":
        return None
    if ":" in cleaned:
        prefix, _, target_id = cleaned.partition(":")
        norm_prefix = prefix.strip().lower()
        norm_id = target_id.strip()
        if norm_prefix in _SUPPORTED_TARGET_PREFIXES and norm_id:
            return [{"type": norm_prefix, "id": norm_id}]
        if norm_prefix in _SUPPORTED_TARGET_PREFIXES and not norm_id:
            raise KnowledgeProviderError(
                "invalid_config",
                f"乐享检索范围格式不合法（缺少目标 ID）: {cleaned}",
                status_code=400,
            )
    return [{"type": "space", "id": cleaned}]


def _extract_space_id(raw_scope: str) -> str:
    cleaned = str(raw_scope or "").strip()
    if not cleaned or cleaned.lower() == "all":
        return ""
    if ":" in cleaned:
        prefix, _, target_id = cleaned.partition(":")
        norm_prefix = prefix.strip().lower()
        norm_id = target_id.strip()
        if norm_prefix == "space":
            return norm_id
        if norm_prefix in _SUPPORTED_TARGET_PREFIXES:
            return ""
    return cleaned


def _extract_space_id_from_url(raw_value: str) -> str:
    cleaned = str(raw_value or "").strip()
    if not cleaned:
        return ""
    match = _LEXIANG_SPACE_URL_RE.search(cleaned)
    if match:
        return match.group(1).strip()
    if "://" in cleaned or "?" in cleaned or "space_id=" in cleaned:
        parsed = urlparse(cleaned)
        query_str = parsed.query if parsed.query else (cleaned if "space_id=" in cleaned else "")
        values = parse_qs(query_str).get("space_id") or []
        for val in values:
            candidate = str(val or "").strip()
            if candidate:
                return candidate
    return ""


def _make_all_scope() -> RemoteScope:
    return RemoteScope(
        id="all",
        name="全站有权限知识",
        description="不限定具体知识库，按 x-staff-id 权限检索全站",
        path="all",
    )


class _TokenCache:
    """Thread-safe in-memory OAuth2 token cache keyed by source and credential digest."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._entries: dict[tuple[int, str, str, str], tuple[str, float]] = {}

    def get(self, key: tuple[int, str, str, str]) -> str | None:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            token, expires_at = entry
            if now >= expires_at:
                self._entries.pop(key, None)
                return None
            return token

    def set(
        self,
        key: tuple[int, str, str, str],
        access_token: str,
        expires_in: float,
    ) -> None:
        ttl = max(_TOKEN_MIN_TTL_SEC, float(expires_in) - _TOKEN_REFRESH_MARGIN_SEC)
        expires_at = self._clock() + ttl
        with self._lock:
            self._entries[key] = (access_token, expires_at)

    def delete(self, key: tuple[int, str, str, str]) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class LexiangProvider:
    provider_type = "lexiang"

    def __init__(
        self,
        *,
        circuit_breaker: SourceCircuitBreaker | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._circuit = circuit_breaker or knowledge_circuit_breaker
        self._default_transport = transport
        self._token_cache = _TokenCache(clock=clock)

    def info(self) -> ProviderTypeInfo:
        return ProviderTypeInfo(
            provider_type=self.provider_type,
            display_name="腾讯乐享知识库",
            description="对接腾讯乐享企业知识库，通过 OAuth2 鉴权与 AI 搜索按库/团队检索，直接展示 Markdown 命中片段并支持跳转原文档。",
            capabilities=ProviderCapabilities(
                supports_remote_scopes=True,
                supports_file_list=False,
                supports_file_content=False,
                supports_synonyms=False,
                supported_auth_types=["oauth2_client_credentials"],
            ),
            config_fields=[
                ProviderFieldSpec(
                    key="staff_id",
                    label="成员帐号 (x-staff-id)",
                    field_type="text",
                    required=True,
                    placeholder="发起检索的乐享成员帐号（或 system-bot）",
                ),
                ProviderFieldSpec(
                    key="team_id",
                    label="团队 ID / 知识库链接（可选）",
                    field_type="text",
                    default="",
                    required=False,
                    placeholder="直接粘贴乐享知识库页面链接，或填写团队 ID / 知识库 ID",
                ),
                ProviderFieldSpec(
                    key="external_project_id",
                    label="检索范围 (Scope)",
                    field_type="text",
                    default="all",
                    required=True,
                    help_text="先点击「测试连接并拉取远端知识库」，再直接在此下拉选择目标知识库",
                ),
                ProviderFieldSpec(
                    key="external_project_name",
                    label="检索范围名称",
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
                    default=20.0,
                    required=False,
                ),
            ],
            credential_fields_by_auth={
                "oauth2_client_credentials": [
                    ProviderFieldSpec(
                        key="app_key",
                        label="AppKey",
                        field_type="text",
                        required=True,
                        placeholder="乐享管理后台【开发 → 接口凭证管理】获取的 AppKey",
                    ),
                    ProviderFieldSpec(
                        key="app_secret",
                        label="AppSecret",
                        field_type="password",
                        required=True,
                        placeholder="对应的 AppSecret",
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

    @staticmethod
    def _extract_credentials(source: ProjectKnowledgeSource) -> tuple[str, str]:
        auth_type = str(source.auth_type or "oauth2_client_credentials").strip().lower()
        if auth_type != "oauth2_client_credentials":
            raise KnowledgeProviderError(
                "invalid_config",
                f"腾讯乐享不支持的认证方式: {auth_type}",
                status_code=400,
            )
        creds = parse_json_dict(source.credentials_json)
        app_key = str(creds.get("app_key") or "").strip()
        app_secret = str(creds.get("app_secret") or "").strip()
        if not app_key or not app_secret:
            raise KnowledgeProviderError(
                "invalid_config",
                "未配置腾讯乐享 AppKey 或 AppSecret",
                status_code=400,
            )
        return app_key, app_secret

    @staticmethod
    def _token_cache_key(
        source: ProjectKnowledgeSource,
        base_url: str,
        app_key: str,
        app_secret: str,
    ) -> tuple[int, str, str, str]:
        secret_digest = hashlib.sha256(app_secret.encode("utf-8")).hexdigest()
        return (int(source.id or 0), base_url, app_key, secret_digest)

    def _fetch_access_token(
        self,
        source: ProjectKnowledgeSource,
        base_url: str,
        app_key: str,
        app_secret: str,
        timeout_sec: float,
        *,
        force_refresh: bool = False,
        use_circuit_breaker: bool = True,
        max_retries: int = 1,
        transport: httpx.BaseTransport | None = None,
    ) -> str:
        cache_key = self._token_cache_key(source, base_url, app_key, app_secret)
        if not force_refresh:
            cached = self._token_cache.get(cache_key)
            if cached:
                return cached

        cb_key = (int(source.id or 0), base_url)
        url = f"{base_url}/cgi-bin/token"
        transient_attempt = 0

        while True:
            if use_circuit_breaker and not self._circuit.allow_request(cb_key):
                raise KnowledgeProviderError(
                    "circuit_open",
                    "腾讯乐享服务暂不可用（已触发熔断保护），请稍后重试",
                )
            try:
                with self._build_client(url, timeout_sec, transport=transport) as client:
                    resp = client.post(
                        url,
                        json={
                            "grant_type": "client_credentials",
                            "app_key": app_key,
                            "app_secret": app_secret,
                        },
                    )
            except httpx.TimeoutException as exc:
                if transient_attempt < max_retries:
                    transient_attempt += 1
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                if use_circuit_breaker:
                    self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError(
                    "timeout",
                    "腾讯乐享 Token 获取超时",
                    status_code=504,
                ) from exc
            except httpx.HTTPError as exc:
                if transient_attempt < max_retries:
                    transient_attempt += 1
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                if use_circuit_breaker:
                    self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError(
                    "connection_error",
                    "无法连接腾讯乐享认证服务",
                    status_code=502,
                ) from exc

            if resp.status_code in {400, 401}:
                detail = _safe_error_detail(
                    resp,
                    "腾讯乐享 AppKey 或 AppSecret 无效，认证失败",
                    secret=app_secret,
                )
                raise KnowledgeProviderError(
                    "auth_failed",
                    detail,
                    status_code=resp.status_code,
                )

            if resp.status_code == 429:
                raise KnowledgeProviderError(
                    "http_error",
                    "腾讯乐享 Token 接口触发频率限制（HTTP 429），请稍后重试",
                    status_code=429,
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
                    f"腾讯乐享认证服务暂时不可用（HTTP {resp.status_code}）",
                    status_code=resp.status_code,
                )

            if resp.status_code >= 500:
                if use_circuit_breaker:
                    self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError(
                    "http_error",
                    _safe_error_detail(
                        resp,
                        f"腾讯乐享认证服务异常（HTTP {resp.status_code}）",
                        secret=app_secret,
                    ),
                    status_code=resp.status_code,
                )

            if resp.status_code >= 400:
                raise KnowledgeProviderError(
                    "http_error",
                    _safe_error_detail(
                        resp,
                        f"腾讯乐享 Token 请求失败（HTTP {resp.status_code}）",
                        secret=app_secret,
                    ),
                    status_code=resp.status_code,
                )

            try:
                data = resp.json()
            except ValueError as exc:
                if use_circuit_breaker:
                    self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError(
                    "http_error",
                    "腾讯乐享 Token 接口返回了无效的 JSON 响应",
                    status_code=502,
                ) from exc

            token = str(data.get("access_token") or "").strip() if isinstance(data, dict) else ""
            if not token:
                raise KnowledgeProviderError(
                    "auth_failed",
                    _extract_error_message(
                        data,
                        "腾讯乐享 Token 响应缺少 access_token",
                        secret=app_secret,
                    ),
                    status_code=401,
                )

            raw_expires = data.get("expires_in", _DEFAULT_TOKEN_EXPIRES_IN)
            try:
                expires_in = float(raw_expires)
            except (TypeError, ValueError):
                expires_in = float(_DEFAULT_TOKEN_EXPIRES_IN)

            self._token_cache.set(cache_key, token, expires_in)
            self._circuit.record_success(cb_key)
            return token

    def _request_json(
        self,
        source: ProjectKnowledgeSource,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        force_refresh_token: bool = False,
        use_circuit_breaker: bool = True,
        max_retries: int = 1,
        transport: httpx.BaseTransport | None = None,
    ) -> Any:
        base_url = validate_http_base_url(source.base_url)
        app_key, app_secret = self._extract_credentials(source)
        cfg = parse_json_dict(source.config_json)
        timeout_sec = _clamp_timeout(cfg.get("timeout_sec"), default=20.0)
        cb_key = (int(source.id or 0), base_url)
        cache_key = self._token_cache_key(source, base_url, app_key, app_secret)

        if use_circuit_breaker and not self._circuit.allow_request(cb_key):
            raise KnowledgeProviderError(
                "circuit_open",
                "腾讯乐享服务暂不可用（已触发熔断保护），请稍后重试",
            )

        token = self._fetch_access_token(
            source,
            base_url,
            app_key,
            app_secret,
            timeout_sec,
            force_refresh=force_refresh_token,
            use_circuit_breaker=use_circuit_breaker,
            max_retries=max_retries,
            transport=transport,
        )

        url = f"{base_url}{path}"
        transient_attempt = 0
        auth_retried = False

        while True:
            if use_circuit_breaker and not self._circuit.allow_request(cb_key):
                raise KnowledgeProviderError(
                    "circuit_open",
                    "腾讯乐享服务暂不可用（已触发熔断保护），请稍后重试",
                )
            headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
            if extra_headers:
                headers.update(extra_headers)
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
                raise KnowledgeProviderError("timeout", "腾讯乐享请求超时", status_code=504) from exc
            except httpx.HTTPError as exc:
                if transient_attempt < max_retries:
                    transient_attempt += 1
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                if use_circuit_breaker:
                    self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError("connection_error", "无法连接腾讯乐享服务", status_code=502) from exc

            if resp.status_code == 401:
                if not auth_retried:
                    auth_retried = True
                    self._token_cache.delete(cache_key)
                    token = self._fetch_access_token(
                        source,
                        base_url,
                        app_key,
                        app_secret,
                        timeout_sec,
                        force_refresh=True,
                        use_circuit_breaker=use_circuit_breaker,
                        max_retries=max_retries,
                        transport=transport,
                    )
                    continue
                raise KnowledgeProviderError(
                    "auth_failed",
                    _safe_error_detail(
                        resp,
                        "腾讯乐享鉴权失败，请检查 AppKey、AppSecret 及成员帐号权限",
                        secret=app_secret,
                    ),
                    status_code=401,
                )

            if resp.status_code == 403:
                raise KnowledgeProviderError(
                    "auth_failed",
                    _safe_error_detail(
                        resp,
                        "腾讯乐享权限不足，请检查接口权限及 x-staff-id 成员权限",
                        secret=app_secret,
                    ),
                    status_code=403,
                )

            if resp.status_code == 429:
                raise KnowledgeProviderError(
                    "http_error",
                    "腾讯乐享接口触发频率限制（HTTP 429），请稍后重试",
                    status_code=429,
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
                    f"腾讯乐享服务暂时不可用（HTTP {resp.status_code}）",
                    status_code=resp.status_code,
                )

            if resp.status_code >= 500:
                if use_circuit_breaker:
                    self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError(
                    "http_error",
                    _safe_error_detail(
                        resp,
                        f"腾讯乐享服务异常（HTTP {resp.status_code}）",
                        secret=app_secret,
                    ),
                    status_code=resp.status_code,
                )

            if resp.status_code >= 400:
                raise KnowledgeProviderError(
                    "http_error",
                    _safe_error_detail(
                        resp,
                        f"腾讯乐享请求失败（HTTP {resp.status_code}）",
                        secret=app_secret,
                    ),
                    status_code=resp.status_code,
                )

            try:
                data = resp.json()
            except ValueError as exc:
                if use_circuit_breaker:
                    self._circuit.record_failure(cb_key)
                raise KnowledgeProviderError(
                    "http_error",
                    "腾讯乐享返回了无效的 JSON 响应",
                    status_code=502,
                ) from exc

            self._circuit.record_success(cb_key)
            return data

    def _fetch_team_spaces(
        self,
        source: ProjectKnowledgeSource,
        team_id: str,
        *,
        team_label: str = "",
        force_refresh_token: bool = False,
        use_circuit_breaker: bool = True,
    ) -> list[RemoteScope]:
        encoded_team = quote(team_id, safe="")
        payload = self._request_json(
            source,
            "GET",
            f"/cgi-bin/v1/kb/spaces?team_id={encoded_team}&limit=100",
            force_refresh_token=force_refresh_token,
            use_circuit_breaker=use_circuit_breaker,
        )
        raw_items = payload.get("data") if isinstance(payload, dict) else payload
        display_team = team_label or team_id
        space_scopes: list[RemoteScope] = []
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                space_id = str(item.get("id") or "").strip()
                if not space_id:
                    continue
                attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
                space_name = str(attrs.get("name") or item.get("name") or space_id).strip()
                space_scopes.append(
                    RemoteScope(
                        id=f"space:{space_id}",
                        name=space_name,
                        description=f"团队 {display_team} 知识库",
                        path="space",
                        raw=item,
                    )
                )
        return space_scopes

    def _collect_scopes_by_team(
        self,
        source: ProjectKnowledgeSource,
        team_id: str,
        *,
        force_refresh_token: bool = False,
        use_circuit_breaker: bool = True,
    ) -> list[RemoteScope]:
        space_scopes = self._fetch_team_spaces(
            source,
            team_id,
            force_refresh_token=force_refresh_token,
            use_circuit_breaker=use_circuit_breaker,
        )
        return [
            _make_all_scope(),
            RemoteScope(
                id=f"team:{team_id}",
                name=f"团队全部知识 ({team_id})",
                description="检索该团队下所有知识库",
                path="team",
            ),
            *space_scopes,
        ]

    def _collect_scopes_by_space(
        self,
        source: ProjectKnowledgeSource,
        space_id: str,
        *,
        force_refresh_token: bool = False,
        use_circuit_breaker: bool = True,
    ) -> list[RemoteScope]:
        encoded_space = quote(space_id, safe="")
        payload = self._request_json(
            source,
            "GET",
            f"/cgi-bin/v1/kb/spaces/{encoded_space}",
            force_refresh_token=force_refresh_token,
            use_circuit_breaker=use_circuit_breaker,
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            data = {}
        resolved_space_id = str(data.get("id") or space_id).strip() or space_id
        attrs = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
        space_name = str(attrs.get("name") or data.get("name") or resolved_space_id).strip()

        relationships = data.get("relationships") if isinstance(data.get("relationships"), dict) else {}
        team_rel = relationships.get("team") if isinstance(relationships.get("team"), dict) else {}
        team_rel_data = team_rel.get("data") if isinstance(team_rel.get("data"), dict) else {}
        derived_team_id = str(team_rel_data.get("id") or "").strip()

        derived_team_name = ""
        included = payload.get("included") if isinstance(payload, dict) else None
        if derived_team_id and isinstance(included, list):
            for inc in included:
                if not isinstance(inc, dict):
                    continue
                if str(inc.get("id") or "").strip() == derived_team_id:
                    inc_attrs = inc.get("attributes") if isinstance(inc.get("attributes"), dict) else {}
                    derived_team_name = str(inc_attrs.get("name") or inc.get("name") or "").strip()
                    if derived_team_name:
                        break

        team_label = derived_team_name or derived_team_id
        primary_scope = RemoteScope(
            id=f"space:{resolved_space_id}",
            name=space_name,
            description=f"团队 {team_label} 知识库" if team_label else "乐享知识库",
            path="space",
            raw=data,
        )

        scopes: list[RemoteScope] = [primary_scope]
        seen_ids: set[str] = {primary_scope.id}

        if derived_team_id:
            try:
                team_spaces = self._fetch_team_spaces(
                    source,
                    derived_team_id,
                    team_label=team_label,
                    force_refresh_token=False,
                    use_circuit_breaker=use_circuit_breaker,
                )
            except KnowledgeProviderError as exc:
                if exc.status_code not in {403, 404}:
                    raise
                team_spaces = []

            for item in team_spaces:
                if item.id in seen_ids:
                    continue
                seen_ids.add(item.id)
                scopes.append(item)

            scopes.append(
                RemoteScope(
                    id=f"team:{derived_team_id}",
                    name=f"团队全部知识 ({team_label})",
                    description="检索该团队下所有知识库",
                    path="team",
                )
            )

        scopes.append(_make_all_scope())
        return scopes

    def _collect_scopes(
        self,
        source: ProjectKnowledgeSource,
        *,
        force_refresh_token: bool = False,
        use_circuit_breaker: bool = True,
    ) -> tuple[list[RemoteScope], bool]:
        base_url = validate_http_base_url(source.base_url)
        app_key, app_secret = self._extract_credentials(source)
        cfg = parse_json_dict(source.config_json)
        timeout_sec = _clamp_timeout(cfg.get("timeout_sec"), default=20.0)
        raw_input = str(cfg.get("team_id") or "").strip()

        if not raw_input:
            self._fetch_access_token(
                source,
                base_url,
                app_key,
                app_secret,
                timeout_sec,
                force_refresh=force_refresh_token,
                use_circuit_breaker=use_circuit_breaker,
            )
            return [_make_all_scope()], False

        lower_input = raw_input.lower()
        if lower_input.startswith("space:"):
            space_id = raw_input.split(":", 1)[1].strip()
            if not space_id:
                raise KnowledgeProviderError(
                    "invalid_config",
                    f"乐享知识库标识格式不合法（缺少知识库 ID）: {raw_input}",
                    status_code=400,
                )
            return (
                self._collect_scopes_by_space(
                    source,
                    space_id,
                    force_refresh_token=force_refresh_token,
                    use_circuit_breaker=use_circuit_breaker,
                ),
                True,
            )

        if lower_input.startswith("team:"):
            team_id = raw_input.split(":", 1)[1].strip()
            if not team_id:
                raise KnowledgeProviderError(
                    "invalid_config",
                    f"乐享团队标识格式不合法（缺少团队 ID）: {raw_input}",
                    status_code=400,
                )
            return (
                self._collect_scopes_by_team(
                    source,
                    team_id,
                    force_refresh_token=force_refresh_token,
                    use_circuit_breaker=use_circuit_breaker,
                ),
                True,
            )

        url_space_id = _extract_space_id_from_url(raw_input)
        if url_space_id:
            return (
                self._collect_scopes_by_space(
                    source,
                    url_space_id,
                    force_refresh_token=force_refresh_token,
                    use_circuit_breaker=use_circuit_breaker,
                ),
                True,
            )

        if "://" in raw_input:
            raise KnowledgeProviderError(
                "invalid_config",
                "未能从粘贴的乐享链接中识别出知识库 ID，请粘贴包含 /spaces/<ID> 或 space_id 参数的知识库页面链接",
                status_code=400,
            )

        try:
            scopes = self._collect_scopes_by_team(
                source,
                raw_input,
                force_refresh_token=force_refresh_token,
                use_circuit_breaker=use_circuit_breaker,
            )
            return scopes, True
        except KnowledgeProviderError as exc:
            if exc.status_code != 404:
                raise
            scopes = self._collect_scopes_by_space(
                source,
                raw_input,
                force_refresh_token=False,
                use_circuit_breaker=use_circuit_breaker,
            )
            return scopes, True

    def list_remote_scopes(self, source: ProjectKnowledgeSource) -> list[RemoteScope]:
        scopes, _ = self._collect_scopes(
            source,
            force_refresh_token=False,
            use_circuit_breaker=True,
        )
        return scopes

    def test_connection(self, source: ProjectKnowledgeSource) -> dict[str, Any]:
        scopes, has_input = self._collect_scopes(
            source,
            force_refresh_token=True,
            use_circuit_breaker=False,
        )
        space_scopes = [scope for scope in scopes if scope.path == "space"]
        if not has_input:
            message = "连接成功；如需按指定知识库检索，请粘贴乐享知识库链接或填写团队 ID 后重新拉取"
        else:
            message = f"连接成功，发现 {len(space_scopes)} 个知识库"
        return {
            "ok": True,
            "message": message,
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
        staff_id = str(cfg.get("staff_id") or "").strip()
        if not staff_id:
            raise KnowledgeProviderError(
                "invalid_config",
                "未配置腾讯乐享成员帐号 (x-staff-id)",
                status_code=400,
            )

        clean_query = str(query or "").strip()
        if not clean_query:
            return []
        if len(clean_query) > _QUERY_MAX_LEN:
            clean_query = clean_query[:_QUERY_MAX_LEN]

        limit = _clamp_top_k(top_k if top_k is not None else cfg.get("top_k"), default=6)
        targets = _parse_targets(str(cfg.get("external_project_id") or ""))

        body: dict[str, Any] = {
            "query": clean_query,
            "top_n": limit,
            "with_score": True,
            "include_fields": ["block_id"],
        }
        if targets:
            body["targets"] = targets

        payload = self._request_json(
            source,
            "POST",
            "/cgi-bin/v1/ai/search",
            json_body=body,
            extra_headers={"x-staff-id": staff_id},
        )
        if not isinstance(payload, dict):
            raise KnowledgeProviderError(
                "http_error",
                "腾讯乐享搜索响应格式异常",
                status_code=502,
            )

        code = payload.get("code")
        if code is not None and code != 0:
            detail = _extract_error_message(
                payload,
                f"腾讯乐享搜索接口返回错误（code={code}）",
            )
            raise KnowledgeProviderError("http_error", detail, status_code=502)

        data = payload.get("data")
        raw_items = data.get("list") if isinstance(data, dict) else None
        if not isinstance(raw_items, list):
            return []

        parsed_items: list[tuple[int, dict[str, Any], float]] = []
        for idx, item in enumerate(raw_items):
            if not isinstance(item, dict):
                continue
            parsed_items.append((idx, item, _parse_raw_score(item.get("score"))))

        max_score = max((raw_score for _, _, raw_score in parsed_items), default=0.0)
        should_normalize = max_score > 1.0

        weight = float(source.weight if source.weight is not None else 1.0)
        hits: list[KnowledgeHit] = []
        for idx, item, raw_score in parsed_items:
            title = str(item.get("title") or "").strip() or "未命名文档"
            content = str(item.get("content") or "").strip()
            raw_url = str(item.get("url") or "").strip()
            document_path = raw_url or title
            chunk_id = str(item.get("block_id") or "").strip() or f"lexiang-{idx}"
            page_no = _parse_first_page_no(item.get("file_pages"))
            score = round(raw_score / max_score, 6) if should_normalize else raw_score

            hits.append(
                KnowledgeHit(
                    source_id=source.id,
                    source_name=source.name,
                    provider_type=self.provider_type,
                    chunk_id=chunk_id,
                    title=title,
                    file_path="",
                    document_path=document_path,
                    page_no=page_no,
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
            "腾讯乐享知识库不支持文件树浏览，请使用 AI 搜索并直接查看命中片段或跳转原文档",
            status_code=400,
        )

    def read_file_content(
        self,
        source: ProjectKnowledgeSource,
        file_path: str,
    ) -> RemoteFileContent:
        raise KnowledgeProviderError(
            "unsupported",
            "腾讯乐享知识库不支持拉取完整文档正文，请直接查看命中片段或点击跳转乐享原文档",
            status_code=400,
        )
