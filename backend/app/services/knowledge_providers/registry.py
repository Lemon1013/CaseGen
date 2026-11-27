"""Registry for pluggable external knowledge base providers."""

from __future__ import annotations

from app.services.knowledge_providers.base import (
    ExternalKnowledgeProvider,
    KnowledgeProviderError,
    ProviderTypeInfo,
)
from app.services.knowledge_providers.lexiang import LexiangProvider
from app.services.knowledge_providers.llm_wiki_django import LlmWikiDjangoProvider
from app.services.knowledge_providers.weknora import WeKnoraProvider

PROVIDER_REGISTRY: dict[str, ExternalKnowledgeProvider] = {}


def register_provider(provider: ExternalKnowledgeProvider) -> None:
    provider_type = str(getattr(provider, "provider_type", "") or "").strip()
    if not provider_type:
        raise ValueError("Provider must define a non-empty provider_type")
    PROVIDER_REGISTRY[provider_type] = provider


def get_provider(provider_type: str) -> ExternalKnowledgeProvider:
    key = str(provider_type or "").strip()
    provider = PROVIDER_REGISTRY.get(key)
    if provider is None:
        raise KnowledgeProviderError(
            "unsupported",
            f"不支持的外部知识库类型: {key or '(empty)'}",
            status_code=400,
        )
    return provider


def list_provider_types() -> list[ProviderTypeInfo]:
    return [provider.info() for provider in PROVIDER_REGISTRY.values()]


register_provider(LlmWikiDjangoProvider())
register_provider(WeKnoraProvider())
register_provider(LexiangProvider())
