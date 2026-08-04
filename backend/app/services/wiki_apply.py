"""Validate and apply Step B Wiki candidates through the revisioned repository."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from sqlmodel import Session

from app.models.entities import WikiReviewItem
from app.services.wiki_plan import PageOperation, StepAPlan, coerce_step_a_plan
from app.services.wiki_repository import WikiPageNotFoundError, WikiRepository
from app.services.wiki_schema import WikiFrontmatter, WikiPage, WikiSource, validate_page_key


MAX_APPLY_PAGES = 8
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
_NUMBER_RE = re.compile(r"(?<![\d.])-?\d+(?:\.\d+)?%?(?![\d.])")


@dataclass
class WikiApplyResult:
    applied_page_keys: list[str] = field(default_factory=list)
    noop_page_keys: list[str] = field(default_factory=list)
    review_item_ids: list[int] = field(default_factory=list)
    source_summary_key: str = ""


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _anchors_to_source(operation: PageOperation, document_id: int) -> WikiSource:
    chunks: list[int] = []
    clauses: list[str] = []
    for anchor in operation.source_anchors:
        for chunk_id in anchor.chunk_ids:
            if chunk_id not in chunks:
                chunks.append(chunk_id)
        for clause in anchor.clause_ids:
            if clause not in clauses:
                clauses.append(clause)
    return WikiSource(document_id=document_id, chunk_ids=chunks, clauses=clauses)


def _merge_sources(existing: Iterable[WikiSource], incoming: Iterable[WikiSource]) -> list[WikiSource]:
    merged: dict[int, WikiSource] = {}
    for source in [*existing, *incoming]:
        current = merged.get(source.document_id)
        if current is None:
            merged[source.document_id] = source.model_copy(deep=True)
            continue
        current.chunk_ids = list(dict.fromkeys([*current.chunk_ids, *source.chunk_ids]))
        current.clauses = _unique([*current.clauses, *source.clauses])
    return list(merged.values())


def _candidate_page(
    raw: Mapping[str, Any],
    *,
    operation: PageOperation,
    document_id: int,
    existing: WikiPage | None = None,
) -> WikiPage:
    key = validate_page_key(str(raw.get("page_key") or operation.page_key))
    if key != operation.page_key:
        raise ValueError(f"candidate key does not match operation: {key}")
    declared_operation = str(raw.get("operation") or "").strip()
    if declared_operation and declared_operation != operation.op:
        raise ValueError(
            f"candidate operation does not match plan for {key}: {declared_operation}"
        )
    page_type = str(raw.get("type") or raw.get("page_type") or operation.page_type or "").strip()
    if existing is not None:
        page_type = page_type or existing.type
        if page_type != existing.type:
            raise ValueError(f"page type cannot change during update: {key}")
    if not page_type:
        page_type = "source" if key.startswith("source.") else "rule"
    body = str(raw.get("body") or "").strip()
    if not body:
        raise ValueError(f"candidate body must not be empty: {key}")

    raw_sources = raw.get("sources") or []
    if isinstance(raw_sources, Mapping):
        raw_sources = [raw_sources]
    parsed_sources: list[WikiSource] = []
    for item in raw_sources if isinstance(raw_sources, list) else []:
        if isinstance(item, Mapping) and item.get("document_id") is not None:
            parsed_sources.append(WikiSource.model_validate(item))
    parsed_sources = _merge_sources(parsed_sources, [_anchors_to_source(operation, document_id)])
    if existing is not None:
        parsed_sources = _merge_sources(existing.frontmatter.sources, parsed_sources)

    aliases = _unique(raw.get("aliases") or [])
    tags = _unique(raw.get("tags") or [])
    if existing is not None:
        aliases = _unique([*existing.frontmatter.aliases, *aliases])
        tags = _unique([*existing.frontmatter.tags, *tags])
        if not bool(raw.get("replace_existing")) and existing.body.strip() not in body:
            body = existing.body.strip() + "\n\n## 增量补充\n\n" + body

    frontmatter = WikiFrontmatter(
        page_key=key,
        title=str(raw.get("title") or (existing.title if existing else key)).strip(),
        type=page_type,
        domain=raw.get("domain") if raw.get("domain") is not None else (
            existing.frontmatter.domain if existing else None
        ),
        aliases=aliases,
        tags=tags,
        sources=parsed_sources,
        status=str(raw.get("status") or "published"),
        revision=existing.frontmatter.revision if existing else 1,
    )
    return WikiPage(frontmatter=frontmatter, body=body)


def _fallback_candidate(
    plan: StepAPlan,
    operation: PageOperation,
    *,
    document_id: int,
    existing: WikiPage | None = None,
) -> dict[str, Any]:
    selected = [
        claim.statement
        for claim in plan.claims
        if not operation.claim_ids or claim.claim_id in operation.claim_ids
    ]
    if not selected:
        selected = [claim.statement for claim in plan.claims]
    body = "\n".join([f"# {operation.page_key}", "", *[f"- {item}" for item in selected]])
    if not selected:
        body += "\n- 本来源未提取到可自动发布的规则，请人工复核。"
    return {
        "page_key": operation.page_key,
        "title": existing.title if existing else operation.page_key,
        "type": existing.type if existing else (operation.page_type or "rule"),
        "aliases": [],
        "tags": list(plan.entities[:8]),
        "sources": [_anchors_to_source(operation, document_id).model_dump(mode="json")],
        "body": body,
        "reason": operation.reason or "deterministic fallback",
    }


def build_source_summary_candidate(plan: StepAPlan, document_id: int) -> tuple[PageOperation, dict[str, Any]]:
    key = f"source.document.{document_id}"
    operation = PageOperation(
        op="create",
        page_key=key,
        page_type="source",
        reason="ensure source summary",
        source_anchors=[],
    )
    lines = [f"# {plan.source_summary.title or key}", ""]
    if plan.source_summary.summary:
        lines.extend([plan.source_summary.summary, ""])
    if plan.claims:
        lines.append("## 关键结论")
        lines.extend(f"- {claim.statement}" for claim in plan.claims[:40])
    return operation, {
        "page_key": key,
        "title": plan.source_summary.title or key,
        "type": "source",
        "aliases": [],
        "tags": list(plan.entities[:8]),
        "sources": [{"document_id": document_id, "chunk_ids": [], "clauses": []}],
        "body": "\n".join(lines).strip(),
        "reason": "ensure source summary",
    }


def _validate_wikilinks(body: str, known_keys: set[str]) -> None:
    for raw_key in _WIKILINK_RE.findall(body):
        key = validate_page_key(raw_key.strip())
        if key not in known_keys:
            raise ValueError(f"wikilink target does not exist: {key}")


def _risk_flags(
    plan: StepAPlan,
    operation: PageOperation,
    candidate: Mapping[str, Any],
    existing: WikiPage,
) -> list[str]:
    risks: list[str] = []
    if any(item.page_key in {None, operation.page_key} for item in plan.contradictions):
        risks.append("contradiction")
    if bool(candidate.get("replace_existing")) and existing.body.strip() not in str(candidate.get("body") or ""):
        risks.append("rule_deletion")
    old_numbers = set(_NUMBER_RE.findall(existing.body))
    new_numbers = set(_NUMBER_RE.findall(str(candidate.get("body") or "")))
    if old_numbers and new_numbers and old_numbers != new_numbers:
        risks.append("numeric_change")
    return risks


def _review_candidate(
    raw: Mapping[str, Any],
    *,
    operation: PageOperation,
    document_id: int,
    existing: WikiPage,
) -> dict[str, Any]:
    """Persist a complete, approvable candidate metadata snapshot."""

    candidate = dict(raw)
    candidate["page_key"] = operation.page_key
    candidate["title"] = str(candidate.get("title") or existing.title)
    candidate["type"] = str(
        candidate.get("type") or candidate.get("page_type") or existing.type
    )
    incoming_aliases = candidate.get("aliases") or []
    incoming_tags = candidate.get("tags") or []
    if isinstance(incoming_aliases, str):
        incoming_aliases = [incoming_aliases]
    if isinstance(incoming_tags, str):
        incoming_tags = [incoming_tags]
    candidate["aliases"] = _unique(
        [*existing.frontmatter.aliases, *incoming_aliases]
    )
    candidate["tags"] = _unique([*existing.frontmatter.tags, *incoming_tags])
    parsed_sources: list[WikiSource] = []
    raw_sources = candidate.get("sources") or []
    if isinstance(raw_sources, Mapping):
        raw_sources = [raw_sources]
    for source in raw_sources if isinstance(raw_sources, list) else []:
        if isinstance(source, Mapping) and source.get("document_id") is not None:
            parsed_sources.append(WikiSource.model_validate(source))
    candidate["sources"] = [
        source.model_dump(mode="json")
        for source in _merge_sources(
            existing.frontmatter.sources,
            [*parsed_sources, _anchors_to_source(operation, document_id)],
        )
    ]
    return candidate


def _queue_review(
    session: Session,
    *,
    operation: str,
    page_id: int | None,
    page_key: str,
    job_id: int | None,
    reason: str,
    risks: list[str],
    candidate: Mapping[str, Any] | None = None,
) -> int:
    item = WikiReviewItem(
        page_id=page_id,
        job_id=job_id,
        kind="merge" if operation == "merge" else (risks[0] if risks else "needs_review"),
        reason=reason or ", ".join(risks) or f"{operation} requires review",
        candidate_frontmatter_json=json.dumps(
            {key: value for key, value in (candidate or {}).items() if key != "body"},
            ensure_ascii=False,
            default=str,
        ),
        candidate_content_md=str((candidate or {}).get("body") or "") or None,
        payload_json=json.dumps(
            {"operation": operation, "page_key": page_key, "risk_flags": risks},
            ensure_ascii=False,
        ),
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return int(item.id)


def queue_merge_review(
    session: Session,
    *,
    page_key: str,
    target_page_key: str,
    job_id: int | None = None,
    reason: str = "duplicate pages require reviewed merge",
) -> int:
    return _queue_review(
        session,
        operation="merge",
        page_id=None,
        page_key=validate_page_key(page_key),
        job_id=job_id,
        reason=reason,
        risks=["merge"],
        candidate={"target_page_key": validate_page_key(target_page_key)},
    )


def apply_wiki_plan(
    session: Session,
    plan: StepAPlan | Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    *,
    document_id: int,
    job_id: int | None = None,
    max_pages: int = MAX_APPLY_PAGES,
) -> WikiApplyResult:
    """Apply safe candidates and queue high-risk updates for review."""

    repository = WikiRepository(session)
    existing_rows = repository.list_rows(include_archived=True)
    existing_keys = {row.page_key for row in existing_rows if row.page_key}
    parsed_plan = coerce_step_a_plan(plan, existing_page_keys=existing_keys)
    candidate_list = list(candidates)
    if len(candidate_list) > max_pages:
        raise ValueError(f"too many Wiki candidates: {len(candidate_list)}")
    by_key: dict[str, Mapping[str, Any]] = {}
    for candidate in candidate_list:
        key = validate_page_key(str(candidate.get("page_key") or ""))
        if key in by_key:
            raise ValueError(f"duplicate Wiki candidate: {key}")
        by_key[key] = candidate

    operations = list(parsed_plan.page_operations)
    planned_keys = {operation.page_key for operation in operations if operation.op != "noop"}
    unexpected_keys = set(by_key) - planned_keys
    if unexpected_keys:
        raise ValueError(
            "candidate pages were not requested by Step A: "
            + ", ".join(sorted(unexpected_keys))
        )
    summary_candidates = [item for item in by_key.values() if (item.get("type") or item.get("page_type")) == "source"]
    summary_key = str(summary_candidates[0].get("page_key")) if summary_candidates else ""
    if not summary_key:
        summary_operation, summary_candidate = build_source_summary_candidate(parsed_plan, document_id)
        if summary_operation.page_key in existing_keys:
            summary_operation.op = "update"
        operations.insert(0, summary_operation)
        by_key[summary_operation.page_key] = summary_candidate
        summary_key = summary_operation.page_key

    if len(operations) > max_pages:
        raise ValueError(f"too many Wiki operations: {len(operations)}")
    known_keys = existing_keys | set(by_key)
    # Validate every model-provided link before the first repository commit so
    # an invalid later candidate cannot leave an earlier page applied.
    for operation in operations:
        raw = by_key.get(operation.page_key)
        if raw is not None:
            _validate_wikilinks(str(raw.get("body") or ""), known_keys)
    result = WikiApplyResult(source_summary_key=summary_key)
    row_ids = {row.page_key: row.id for row in existing_rows if row.page_key}
    for item in parsed_plan.review_items:
        review_id = _queue_review(
            session,
            operation="merge" if item.kind == "merge" else "review",
            page_id=row_ids.get(item.page_key) if item.page_key else None,
            page_key=item.page_key or summary_key,
            job_id=job_id,
            reason=item.reason,
            risks=[item.kind],
            candidate={
                "claim_ids": item.claim_ids,
                "severity": item.severity,
            },
        )
        result.review_item_ids.append(review_id)
    operation_keys = {operation.page_key for operation in operations}
    for contradiction in parsed_plan.contradictions:
        if contradiction.page_key in operation_keys:
            continue
        review_id = _queue_review(
            session,
            operation="review",
            page_id=row_ids.get(contradiction.page_key) if contradiction.page_key else None,
            page_key=contradiction.page_key or summary_key,
            job_id=job_id,
            reason=contradiction.description,
            risks=["contradiction"],
            candidate={"claim_ids": contradiction.claim_ids},
        )
        result.review_item_ids.append(review_id)
    for operation in operations:
        if operation.op == "noop":
            result.noop_page_keys.append(operation.page_key)
            continue
        old_record = None
        old_page = None
        if operation.op == "update":
            try:
                old_record = repository.read(operation.page_key)
                old_page = old_record.page
            except WikiPageNotFoundError:
                raise
            except Exception as exc:
                review_id = _queue_review(
                    session,
                    operation="update",
                    page_id=None,
                    page_key=operation.page_key,
                    job_id=job_id,
                    reason=f"existing page cannot be safely read: {exc}",
                    risks=["legacy_or_corrupt_page"],
                )
                result.review_item_ids.append(review_id)
                continue
        raw = by_key.get(operation.page_key) or _fallback_candidate(
            parsed_plan,
            operation,
            document_id=document_id,
            existing=old_page,
        )
        _validate_wikilinks(str(raw.get("body") or ""), known_keys)
        if old_page is not None:
            risks = _risk_flags(parsed_plan, operation, raw, old_page)
            if risks:
                review_candidate = _review_candidate(
                    raw,
                    operation=operation,
                    document_id=document_id,
                    existing=old_page,
                )
                review_id = _queue_review(
                    session,
                    operation="update",
                    page_id=old_record.id if old_record else None,
                    page_key=operation.page_key,
                    job_id=job_id,
                    reason=operation.reason,
                    risks=risks,
                    candidate=review_candidate,
                )
                result.review_item_ids.append(review_id)
                continue
        page = _candidate_page(
            raw,
            operation=operation,
            document_id=document_id,
            existing=old_page,
        )
        reason = operation.reason or str(raw.get("reason") or operation.op)
        if operation.op == "create":
            repository.create(page, job_id=job_id, reason=reason)
        else:
            repository.update(operation.page_key, page, job_id=job_id, reason=reason)
        result.applied_page_keys.append(operation.page_key)
        existing_keys.add(operation.page_key)
    return result


__all__ = [
    "MAX_APPLY_PAGES",
    "WikiApplyResult",
    "apply_wiki_plan",
    "build_source_summary_candidate",
    "queue_merge_review",
]
