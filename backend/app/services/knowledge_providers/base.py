"""Base contracts, schemas, error types and security helpers for external knowledge providers."""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Optional, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from app.models.external_wiki import ProjectKnowledgeSource

ErrorCategory = Literal[
    "auth_failed",
    "timeout",
    "connection_error",
    "http_error",
    "circuit_open",
    "invalid_config",
    "unsupported",
]

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class ProviderFieldSpec(BaseModel):
    key: str
    label: str
    field_type: Literal["text", "password", "number", "boolean", "select", "textarea"] = "text"
    required: bool = False
    default: Any = None
    placeholder: str = ""
    help_text: str = ""
    options: list[dict[str, str]] = Field(default_factory=list)


class ProviderCapabilities(BaseModel):
    supports_remote_scopes: bool = False
    supports_file_list: bool = False
    supports_file_content: bool = False
    supports_synonyms: bool = False
    supported_auth_types: list[str] = Field(default_factory=lambda: ["none"])


class ProviderTypeInfo(BaseModel):
    provider_type: str
    display_name: str
    description: str = ""
    capabilities: ProviderCapabilities = Field(default_factory=ProviderCapabilities)
    config_fields: list[ProviderFieldSpec] = Field(default_factory=list)
    credential_fields_by_auth: dict[str, list[ProviderFieldSpec]] = Field(default_factory=dict)


class RemoteScope(BaseModel):
    id: str
    name: str
    description: str = ""
    path: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


class RemoteFileItem(BaseModel):
    path: str
    name: str
    size: Optional[int] = None
    updated_at: Optional[str] = None


class RemoteFileContent(BaseModel):
    path: str
    content: str
    source_id: Optional[int] = None
    source_name: str = ""


class KnowledgeHit(BaseModel):
    source_id: Optional[int] = None
    source_name: str = ""
    provider_type: str = ""
    chunk_id: int | str = 0
    title: str = ""
    file_path: str = ""
    document_path: str = ""
    heading_path: str = ""
    page_no: Optional[int] = None
    score: float = 0.0
    weighted_score: float = 0.0
    snippet: str = ""
    evidence_snippet: str = ""
    highlight_terms: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class SourceWarning(BaseModel):
    source_id: Optional[int] = None
    source_name: str = ""
    category: str = ""
    message: str = ""


class ProjectKnowledgeSearchResult(BaseModel):
    configured: bool = False
    empty_reason: Literal["none", "not_configured", "no_hits", "all_sources_failed"] = "not_configured"
    message: str = ""
    hits: list[KnowledgeHit] = Field(default_factory=list)
    warnings: list[SourceWarning] = Field(default_factory=list)


class KnowledgeProviderError(Exception):
    """Sanitized provider exception safe to expose to callers and logs."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.status_code = status_code


class ExternalKnowledgeProvider(Protocol):
    provider_type: str

    def info(self) -> ProviderTypeInfo: ...

    def test_connection(self, source: ProjectKnowledgeSource) -> dict[str, Any]: ...

    def list_remote_scopes(self, source: ProjectKnowledgeSource) -> list[RemoteScope]: ...

    def search(
        self,
        source: ProjectKnowledgeSource,
        query: str,
        *,
        top_k: int | None = None,
    ) -> list[KnowledgeHit]: ...

    def list_files(self, source: ProjectKnowledgeSource) -> list[RemoteFileItem]: ...

    def read_file_content(
        self,
        source: ProjectKnowledgeSource,
        file_path: str,
    ) -> RemoteFileContent: ...


def parse_json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw or not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def validate_safe_file_path(file_path: str) -> str:
    """Validate relative remote file paths against traversal or absolute escapes."""
    text_path = str(file_path or "").strip()
    if not text_path or "\x00" in text_path:
        raise KnowledgeProviderError("invalid_config", "文件路径不合法", status_code=400)
    if "\\" in text_path or text_path.startswith("/") or _WINDOWS_DRIVE_RE.match(text_path):
        raise KnowledgeProviderError("invalid_config", "文件路径不合法", status_code=400)
    parts = text_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise KnowledgeProviderError("invalid_config", "文件路径不合法", status_code=400)
    return "/".join(parts)


def validate_http_base_url(base_url: str) -> str:
    """Normalize and validate an HTTP(S) base URL without embedded userinfo."""
    cleaned = str(base_url or "").strip().rstrip("/")
    if not cleaned:
        raise KnowledgeProviderError("invalid_config", "知识库地址不能为空", status_code=400)
    try:
        parsed = urlsplit(cleaned)
    except ValueError as exc:
        raise KnowledgeProviderError("invalid_config", "知识库地址格式不正确", status_code=400) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise KnowledgeProviderError("invalid_config", "知识库地址必须为有效的 http/https URL", status_code=400)
    if parsed.username is not None or parsed.password is not None:
        raise KnowledgeProviderError("invalid_config", "知识库地址不允许嵌入账号或密码", status_code=400)
    return cleaned


def mask_credentials(auth_type: str, creds: Any) -> dict[str, Any]:
    """Return non-sensitive credential metadata only; never expose secrets."""
    parsed = parse_json_dict(creds)
    normalized_auth = str(auth_type or "none").strip().lower()
    if normalized_auth == "django_session":
        username = str(parsed.get("username") or "").strip()
        has_secret = bool(str(parsed.get("password") or ""))
        return {
            "has_credentials": bool(username or has_secret),
            "username": username,
        }
    if normalized_auth == "bearer":
        return {
            "has_credentials": bool(str(parsed.get("token") or "")),
            "username": "",
        }
    if normalized_auth == "api_key":
        return {
            "has_credentials": bool(str(parsed.get("api_key") or parsed.get("key") or "")),
            "username": "",
        }
    if normalized_auth == "oauth2_client_credentials":
        app_key = str(parsed.get("app_key") or "").strip()
        has_secret = bool(str(parsed.get("app_secret") or "").strip())
        return {
            "has_credentials": has_secret,
            "username": app_key,
        }
    return {"has_credentials": False, "username": ""}
