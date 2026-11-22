"""Retrieval strategy protocol, default external-knowledge adapter, and checkpoint stage."""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from sqlmodel import Session, func, select

from app.models.entities import (
    GenerationTask,
    Project,
    Requirement,
    TaskCitation,
    TaskRetrievalCheckpoint,
    WikiSpace,
)
from app.services.knowledge_providers import (
    ProjectKnowledgeSearchResult,
    search_project_knowledge,
)
from app.services.pipeline.common import _get_facade, _set_status
from app.services.pipeline.context import _build_query, assemble_task_context
from app.services.task_events import append_event
from app.services.task_stream import task_stream


@runtime_checkable
class RetrievalStrategy(Protocol):
    """Pluggable knowledge retrieval contract.

    Default execution delegates to configured external knowledge providers;
    future Agent multi-round autonomous retrieval can plug in by implementing
    this protocol and registering via ``set_retrieval_strategy`` or passing
    ``retrieval_strategy=...`` to ``run_generate``.
    """

    def retrieve(
        self,
        session: Session,
        *,
        project_id: int | None,
        query: str,
        top_k: int | None = None,
    ) -> ProjectKnowledgeSearchResult:
        ...


class ExternalKnowledgeRetrievalStrategy:
    """Default retrieval strategy delegating to project external knowledge providers."""

    def retrieve(
        self,
        session: Session,
        *,
        project_id: int | None,
        query: str,
        top_k: int | None = None,
    ) -> ProjectKnowledgeSearchResult:
        if project_id is None:
            return ProjectKnowledgeSearchResult(
                configured=False,
                empty_reason="not_configured",
                message="该项目未配置知识库，可联系管理员添加",
                hits=[],
                warnings=[],
            )
        # Honor monkeypatches applied on the task_pipeline facade for test compatibility.
        facade = _get_facade()
        search_fn = (
            getattr(facade, "search_project_knowledge", None)
            if facade is not None
            else None
        )
        if not callable(search_fn):
            search_fn = search_project_knowledge
        if top_k is None:
            return search_fn(session, int(project_id), query)
        return search_fn(session, int(project_id), query, top_k=top_k)


_DEFAULT_RETRIEVAL_STRATEGY: RetrievalStrategy = ExternalKnowledgeRetrievalStrategy()
_current_retrieval_strategy: RetrievalStrategy = _DEFAULT_RETRIEVAL_STRATEGY


def get_retrieval_strategy() -> RetrievalStrategy:
    """Return the active process-wide retrieval strategy."""
    return _current_retrieval_strategy


def set_retrieval_strategy(strategy: RetrievalStrategy | None) -> None:
    """Override the process-wide retrieval strategy (pass None to restore default)."""
    global _current_retrieval_strategy
    _current_retrieval_strategy = (
        _DEFAULT_RETRIEVAL_STRATEGY if strategy is None else strategy
    )


def _clear_citations(session: Session, task_id: int) -> None:
    rows = session.exec(select(TaskCitation).where(TaskCitation.task_id == task_id)).all()
    for row in rows:
        session.delete(row)
    session.flush()


def load_confirmed_retrieval_context(
    checkpoint: TaskRetrievalCheckpoint,
) -> tuple[str, dict[str, Any], str, list[int]]:
    """Rebuild selected context from a confirmed retrieval checkpoint.

    Returns ``(query, assembled_context, supplemental_text, selected_citation_ids)``.
    """
    payload = json.loads(checkpoint.retrieval_json or "{}")
    if not isinstance(payload, dict) or not isinstance(payload.get("context"), dict):
        raise ValueError("Invalid retrieval checkpoint payload")
    all_context = payload.get("context") or {}
    raw_selected = json.loads(checkpoint.selected_citation_ids_json or "[]")
    selected_ids = {str(item) for item in raw_selected}
    selected_citations = {
        str(item.get("task_citation_id"))
        for item in (all_context.get("citations") or [])
        if str(item.get("task_citation_id")) in selected_ids
    }
    if selected_ids - selected_citations:
        raise ValueError("Selected citation is missing from retrieval checkpoint")

    wiki_hits = [
        item
        for item in all_context.get("wiki_hits", [])
        if str(item.get("task_citation_id")) in selected_citations
    ]
    source_hits = [
        item
        for item in all_context.get("source_hits", [])
        if str(item.get("task_citation_id")) in selected_citations
    ]
    external_hits = [
        item
        for item in all_context.get("external_hits", [])
        if str(item.get("task_citation_id")) in selected_citations
    ]
    context = assemble_task_context(
        wiki_hits,
        source_hits,
        external_hits=external_hits,
        query=checkpoint.query,
        include_explain=True,
    )
    selected_int_ids: list[int] = []
    for item in raw_selected:
        try:
            selected_int_ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return (
        checkpoint.query,
        context,
        checkpoint.supplemental_text or "",
        list(dict.fromkeys(selected_int_ids)),
    )


def run_retrieval_stage(
    session: Session,
    task: GenerationTask,
    requirement: Requirement,
    *,
    stream_task_id: int,
    auto_review: bool = False,
    retrieval_strategy: RetrievalStrategy | None = None,
) -> GenerationTask:
    """Execute knowledge retrieval, persist TaskCitations and a pending checkpoint."""
    task_stream.status(stream_task_id, status="retrieving", message="正在检索外部知识库")
    query = _build_query(requirement)

    if task.wiki_space_id is None and task.project_id is not None:
        project_row = session.get(Project, task.project_id)
        fallback_space_id = project_row.default_wiki_space_id if project_row else None
        if fallback_space_id is None:
            first_space = session.exec(
                select(WikiSpace)
                .where(
                    WikiSpace.project_id == task.project_id,
                    WikiSpace.scope == "project",
                    WikiSpace.status == "active",
                )
                .order_by(WikiSpace.id.asc())
            ).first()
            fallback_space_id = first_space.id if first_space else None
        if fallback_space_id is not None:
            task.wiki_space_id = int(fallback_space_id)
            session.add(task)
            session.commit()

    strategy = retrieval_strategy or get_retrieval_strategy()
    search_res = strategy.retrieve(
        session,
        project_id=int(task.project_id) if task.project_id is not None else None,
        query=query,
    )

    status_map = {
        "none": "ready",
        "not_configured": "not_configured",
        "no_hits": "no_hits",
        "all_sources_failed": "source_error",
    }
    knowledge_status = status_map.get(search_res.empty_reason, "no_hits")
    if knowledge_status == "not_configured":
        knowledge_message = "该项目未配置知识库，可联系管理员添加"
    elif knowledge_status == "no_hits":
        knowledge_message = "未检索到相关内容"
    elif knowledge_status == "source_error":
        knowledge_message = search_res.message or "外部知识库检索失败"
    else:
        knowledge_message = ""
    source_warnings = [w.model_dump() for w in search_res.warnings]

    raw_external_hits = [
        {
            "id": hit.chunk_id,
            "chunk_id": hit.chunk_id,
            "knowledge_source_id": hit.source_id,
            "source_name": hit.source_name,
            "provider_type": hit.provider_type,
            "title": hit.title,
            "path": hit.file_path or hit.document_path,
            "file_path": hit.file_path or hit.document_path,
            "document_path": hit.document_path,
            "heading_path": hit.heading_path,
            "page_no": hit.page_no,
            "score": hit.weighted_score or hit.score,
            "snippet": hit.snippet,
            "evidence_snippet": hit.evidence_snippet,
            "highlight_terms": hit.highlight_terms,
            "external_metadata": {
                "page_no": hit.page_no,
                "chunk_id": hit.chunk_id,
                "source_name": hit.source_name,
                "provider_type": hit.provider_type,
            },
        }
        for hit in search_res.hits
    ]

    context = assemble_task_context(
        [],
        [],
        external_hits=raw_external_hits,
        query=query,
        include_explain=True,
    )
    external_hits = list(context.get("external_hits") or [])
    _clear_citations(session, task.id)
    citation_rows: list[TaskCitation] = []
    for citation in context["citations"]:
        cids = list(citation.get("clause_ids") or [])
        ext_meta = citation.get("external_metadata") or {}
        row = TaskCitation(
            task_id=task.id,
            citation_type=citation.get("citation_type") or "external",
            wiki_page_id=citation.get("wiki_page_id"),
            source_chunk_id=citation.get("source_chunk_id"),
            title=citation.get("title") or "",
            path=citation.get("path") or "",
            score=float(citation.get("score") or 0.0),
            snippet=citation.get("snippet") or "",
            content_excerpt=citation.get("content_excerpt") or "",
            clause_ids_json=json.dumps(cids, ensure_ascii=False),
            anchor_clause=citation.get("anchor_clause"),
            knowledge_source_id=citation.get("knowledge_source_id"),
            file_path=citation.get("file_path") or citation.get("path") or None,
            heading_path=citation.get("heading_path") or None,
            external_metadata_json=(
                json.dumps(ext_meta, ensure_ascii=False)
                if (citation.get("citation_type") == "external" and ext_meta)
                else None
            ),
        )
        session.add(row)
        citation_rows.append(row)
    session.flush()

    for citation, row in zip(context["citations"], citation_rows):
        citation["task_citation_id"] = row.id
    for index, hit in enumerate(external_hits):
        if index < len(context["citations"]):
            hit["task_citation_id"] = context["citations"][index].get("task_citation_id")

    hit_count = len(context["citations"])
    append_event(
        session,
        task.id,
        "retrieve",
        f"检索完成：外部知识 {len(external_hits)} 条",
        detail={
            "query": query,
            "hit_count": hit_count,
            "knowledge_status": knowledge_status,
            "source_warnings": source_warnings,
            "context_budgets": context["budgets"],
            "context_explain": context.get("explain"),
        },
    )
    if hit_count == 0:
        append_event(
            session,
            task.id,
            "retrieve",
            knowledge_message or "未检索到外部知识，用户确认后可继续生成",
        )

    attempt = (
        session.exec(
            select(func.max(TaskRetrievalCheckpoint.attempt)).where(
                TaskRetrievalCheckpoint.task_id == task.id
            )
        ).one()
        or 0
    ) + 1
    snapshot = {
        "context": context,
        "conflict_groups": [],
        "requires_knowledge_decision": False,
        "knowledge_status": knowledge_status,
        "knowledge_message": knowledge_message,
        "source_warnings": source_warnings,
    }
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, default=str)
    checkpoint = TaskRetrievalCheckpoint(
        task_id=task.id,
        attempt=int(attempt),
        status="pending",
        auto_review=auto_review,
        query=query,
        retrieval_json=snapshot_json,
        candidate_citation_ids_json=json.dumps([r.id for r in citation_rows]),
        version=1,
    )
    session.add(checkpoint)
    _set_status(task, "awaiting_confirmation")
    append_event(
        session,
        task.id,
        "retrieve",
        "检索完成，等待人工确认",
        detail={
            "knowledge_status": knowledge_status,
            "requires_knowledge_decision": False,
            "conflict_count": 0,
        },
    )
    session.commit()
    session.refresh(task)
    task_stream.status(
        stream_task_id,
        status="awaiting_confirmation",
        message="检索完成，请确认引用后继续生成",
    )
    return task
