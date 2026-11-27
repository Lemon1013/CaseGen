"""Pluggable external knowledge base provider architecture."""

from app.services.knowledge_providers.base import (
    ExternalKnowledgeProvider,
    KnowledgeHit,
    KnowledgeProviderError,
    ProjectKnowledgeSearchResult,
    ProviderCapabilities,
    ProviderFieldSpec,
    ProviderTypeInfo,
    RemoteFileContent,
    RemoteFileItem,
    RemoteScope,
    SourceWarning,
    mask_credentials,
    parse_json_dict,
    validate_http_base_url,
    validate_safe_file_path,
)
from app.services.knowledge_providers.circuit import (
    SourceCircuitBreaker,
    knowledge_circuit_breaker,
)
from app.services.knowledge_providers.lexiang import LexiangProvider
from app.services.knowledge_providers.llm_wiki_django import LlmWikiDjangoProvider
from app.services.knowledge_providers.weknora import WeKnoraProvider
from app.services.knowledge_providers.registry import (
    PROVIDER_REGISTRY,
    get_provider,
    list_provider_types,
    register_provider,
)
from app.services.knowledge_providers.service import (
    list_source_files,
    read_source_file,
    search_project_knowledge,
)

__all__ = [
    "ExternalKnowledgeProvider",
    "KnowledgeHit",
    "KnowledgeProviderError",
    "LexiangProvider",
    "LlmWikiDjangoProvider",
    "PROVIDER_REGISTRY",
    "ProjectKnowledgeSearchResult",
    "ProviderCapabilities",
    "ProviderFieldSpec",
    "ProviderTypeInfo",
    "RemoteFileContent",
    "RemoteFileItem",
    "RemoteScope",
    "SourceCircuitBreaker",
    "SourceWarning",
    "WeKnoraProvider",
    "get_provider",
    "knowledge_circuit_breaker",
    "list_provider_types",
    "list_source_files",
    "mask_credentials",
    "parse_json_dict",
    "read_source_file",
    "register_provider",
    "search_project_knowledge",
    "validate_http_base_url",
    "validate_safe_file_path",
]
