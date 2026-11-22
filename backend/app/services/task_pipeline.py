"""Thin compatibility facade over the modular ``app.services.pipeline`` package.

All core stage implementations live under ``app.services.pipeline.*``.  This
module retains the five injectable test hooks and ``search_project_knowledge``
attribute so existing callers and test monkeypatches continue working without
breaking imports.
"""

from __future__ import annotations

from typing import Callable, Optional

from app.services.knowledge_providers import search_project_knowledge
from app.services.pipeline import (
    ChatFn,
    ExternalKnowledgeRetrievalStrategy,
    LLMError,
    RetrievalStrategy,
    _LEAN_GENERATE_SYSTEM,
    _append_supplemental_context,
    _build_optimize_messages,
    _build_point_generate_messages,
    _build_review_messages,
    _call_chat,
    _citations_for_point,
    _fail_task,
    _generation_stopped,
    _link_draft_to_test_points,
    _next_draft_version,
    _normalize_point_chunk_case_keys,
    _reference_cases_text,
    _resolve_chat_fn,
    _resolve_default_generate_prompt,
    _resolve_generate_prompt,
    _resolve_model,
    _resolve_prompt_by_type,
    _resolve_review_model,
    _set_status,
    _strip_yaml_frontmatter,
    _test_points_text,
    apply_prompt,
    assemble_task_context,
    build_task_context,
    checkpoint_conflicts,
    clean_retrieve_query,
    finalize_task,
    get_retrieval_strategy,
    load_confirmed_retrieval_context,
    run_generate,
    run_generate_test_points,
    run_optimize_prompt,
    run_point_by_point_generation,
    run_regenerate,
    run_retrieval_stage,
    run_review,
    set_retrieval_strategy,
)

# Optional injectable chat hooks for tests: (messages, model_cfg) -> str.
# Read dynamically by ``app.services.pipeline.common._resolve_chat_fn``.
_PIPELINE_CHAT_FN: Optional[Callable[..., str]] = None
_GENERATE_CHAT_FN: Optional[Callable[..., str]] = None
_TEST_POINTS_CHAT_FN: Optional[Callable[..., str]] = None
_REVIEW_CHAT_FN: Optional[Callable[..., str]] = None
_OPTIMIZE_CHAT_FN: Optional[Callable[..., str]] = None

__all__ = [
    "ChatFn",
    "ExternalKnowledgeRetrievalStrategy",
    "LLMError",
    "RetrievalStrategy",
    "_GENERATE_CHAT_FN",
    "_LEAN_GENERATE_SYSTEM",
    "_OPTIMIZE_CHAT_FN",
    "_PIPELINE_CHAT_FN",
    "_REVIEW_CHAT_FN",
    "_TEST_POINTS_CHAT_FN",
    "_append_supplemental_context",
    "_build_optimize_messages",
    "_build_point_generate_messages",
    "_build_review_messages",
    "_call_chat",
    "_citations_for_point",
    "_fail_task",
    "_generation_stopped",
    "_link_draft_to_test_points",
    "_next_draft_version",
    "_normalize_point_chunk_case_keys",
    "_reference_cases_text",
    "_resolve_chat_fn",
    "_resolve_default_generate_prompt",
    "_resolve_generate_prompt",
    "_resolve_model",
    "_resolve_prompt_by_type",
    "_resolve_review_model",
    "_set_status",
    "_strip_yaml_frontmatter",
    "_test_points_text",
    "apply_prompt",
    "assemble_task_context",
    "build_task_context",
    "checkpoint_conflicts",
    "clean_retrieve_query",
    "finalize_task",
    "get_retrieval_strategy",
    "load_confirmed_retrieval_context",
    "run_generate",
    "run_generate_test_points",
    "run_optimize_prompt",
    "run_point_by_point_generation",
    "run_regenerate",
    "run_retrieval_stage",
    "run_review",
    "search_project_knowledge",
    "set_retrieval_strategy",
]
