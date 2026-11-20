"""Unified external knowledge retrieval and file proxy service."""

from __future__ import annotations

import logging

from sqlmodel import Session, select

from app.models.external_wiki import ProjectKnowledgeSource
from app.services.knowledge_providers.base import (
    KnowledgeHit,
    KnowledgeProviderError,
    ProjectKnowledgeSearchResult,
    RemoteFileContent,
    RemoteFileItem,
    SourceWarning,
    parse_json_dict,
    validate_safe_file_path,
)
from app.services.knowledge_providers.registry import get_provider

logger = logging.getLogger(__name__)


def _is_source_configured(source: ProjectKnowledgeSource) -> bool:
    try:
        provider = get_provider(source.provider_type)
    except KnowledgeProviderError:
        return False
    if provider.info().capabilities.supports_remote_scopes:
        cfg = parse_json_dict(source.config_json)
        return bool(str(cfg.get("external_project_id") or "").strip())
    return True


def _get_project_source(
    session: Session,
    project_id: int,
    source_id: int,
) -> ProjectKnowledgeSource:
    source = session.get(ProjectKnowledgeSource, source_id)
    if source is None or int(source.project_id) != int(project_id):
        raise LookupError("Knowledge source not found")
    return source


def search_project_knowledge(
    session: Session,
    project_id: int,
    query: str,
    *,
    top_k: int | None = None,
) -> ProjectKnowledgeSearchResult:
    """Aggregate and rank external knowledge hits across all enabled sources for a project."""
    sources = session.exec(
        select(ProjectKnowledgeSource)
        .where(
            ProjectKnowledgeSource.project_id == project_id,
            ProjectKnowledgeSource.enabled == True,  # noqa: E712
        )
        .order_by(ProjectKnowledgeSource.priority.asc(), ProjectKnowledgeSource.id.asc())
    ).all()

    configured_sources = [source for source in sources if _is_source_configured(source)]
    if not configured_sources:
        return ProjectKnowledgeSearchResult(
            configured=False,
            empty_reason="not_configured",
            message="该项目未配置知识库，可联系管理员添加",
            hits=[],
            warnings=[],
        )

    all_hits: list[KnowledgeHit] = []
    warnings: list[SourceWarning] = []
    succeeded_count = 0

    for source in configured_sources:
        try:
            provider = get_provider(source.provider_type)
            source_hits = provider.search(source, query, top_k=top_k)
            succeeded_count += 1
            weight = float(source.weight if source.weight is not None else 1.0)
            for hit in source_hits:
                hit.source_id = source.id
                hit.source_name = source.name
                hit.provider_type = source.provider_type
                hit.weighted_score = float(hit.score) * weight
                all_hits.append(hit)
        except KnowledgeProviderError as exc:
            logger.warning(
                "Knowledge source search warning (project=%s source_id=%s category=%s): %s",
                project_id,
                source.id,
                exc.category,
                exc.message,
            )
            warnings.append(
                SourceWarning(
                    source_id=source.id,
                    source_name=source.name,
                    category=exc.category,
                    message=exc.message,
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Unexpected error querying knowledge source (project=%s source_id=%s)",
                project_id,
                source.id,
            )
            warnings.append(
                SourceWarning(
                    source_id=source.id,
                    source_name=source.name,
                    category="http_error",
                    message="外部知识库检索失败",
                )
            )

    all_hits.sort(key=lambda item: (item.weighted_score, item.score), reverse=True)
    deduped_hits: list[KnowledgeHit] = []
    seen_keys: set[tuple[int | None, str, str, str]] = set()
    for hit in all_hits:
        dedup_key = (
            hit.source_id,
            hit.file_path,
            str(hit.chunk_id),
            hit.title,
        )
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        deduped_hits.append(hit)

    if top_k is not None:
        limit = max(1, int(top_k))
    else:
        source_limits = []
        for source in configured_sources:
            cfg = parse_json_dict(source.config_json)
            try:
                source_limits.append(max(1, int(cfg.get("top_k") or 6)))
            except (TypeError, ValueError):
                source_limits.append(6)
        limit = max(source_limits, default=6)
    final_hits = deduped_hits[:limit]

    if final_hits:
        return ProjectKnowledgeSearchResult(
            configured=True,
            empty_reason="none",
            message="",
            hits=final_hits,
            warnings=warnings,
        )
    if succeeded_count == 0 and warnings:
        summary = "; ".join(f"{w.source_name}: {w.message}" for w in warnings)
        return ProjectKnowledgeSearchResult(
            configured=True,
            empty_reason="all_sources_failed",
            message=summary,
            hits=[],
            warnings=warnings,
        )
    return ProjectKnowledgeSearchResult(
        configured=True,
        empty_reason="no_hits",
        message="未检索到相关内容",
        hits=[],
        warnings=warnings,
    )


def list_source_files(
    session: Session,
    project_id: int,
    source_id: int,
) -> list[RemoteFileItem]:
    source = _get_project_source(session, project_id, source_id)
    provider = get_provider(source.provider_type)
    if not provider.info().capabilities.supports_file_list:
        raise KnowledgeProviderError(
            "unsupported",
            "该外部知识库不支持文件列表读取",
            status_code=400,
        )
    return provider.list_files(source)


def read_source_file(
    session: Session,
    project_id: int,
    source_id: int,
    file_path: str,
) -> RemoteFileContent:
    source = _get_project_source(session, project_id, source_id)
    provider = get_provider(source.provider_type)
    if not provider.info().capabilities.supports_file_content:
        raise KnowledgeProviderError(
            "unsupported",
            "该外部知识库不支持文件正文读取",
            status_code=400,
        )
    clean_path = validate_safe_file_path(file_path)
    return provider.read_file_content(source, clean_path)
