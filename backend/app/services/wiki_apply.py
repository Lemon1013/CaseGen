"""Validate and apply Step B Wiki candidates through the revisioned repository."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from sqlmodel import Session

from app.models.entities import WikiReviewItem
from app.services.wiki_plan import PageOperation, StepAPlan, coerce_step_a_plan
from app.services.wiki_repository import (
    WikiAtomicApplyError,
    WikiPageAlreadyExistsError,
    WikiPageCorruptError,
    WikiPageFileError,
    WikiPageNotFoundError,
    WikiRepository,
)
from app.services.wiki_schema import WikiFrontmatter, WikiPage, WikiSource, validate_page_key
from app.services.wiki_titles import display_title


MAX_APPLY_PAGES = 8
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
_NUMBER_RE = re.compile(r"(?<![\d.])-?\d+(?:\.\d+)?%?(?![\d.])")


@dataclass
class WikiApplyResult:
    applied_page_keys: list[str] = field(default_factory=list)
    noop_page_keys: list[str] = field(default_factory=list)
    review_item_ids: list[int] = field(default_factory=list)
    skipped_page_keys: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_summary_key: str = ""


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _string_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return []
    try:
        return [str(item) for item in value]
    except TypeError:
        return [str(value)]


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
    # Identity belongs to the server-side plan.  Step B can only contribute
    # human-facing text and metadata; mismatched model fields are ignored.
    key = validate_page_key(operation.page_key)
    page_type = str(operation.page_type or "").strip()
    if existing is not None:
        page_type = existing.type
    if not page_type:
        raw_type = str(raw.get("type") or raw.get("page_type") or "").strip()
        page_type = raw_type if raw_type in {
            "source", "rule", "entity", "scenario", "regression", "synthesis"
        } else ("source" if key.startswith("source.") else "rule")
    body = str(raw.get("body") or "").strip()
    if not body:
        raise ValueError(f"candidate body must not be empty: {key}")

    # Source evidence is bound from the actual analyze window.  Never trust a
    # model-provided document/chunk id at the repository boundary.
    parsed_sources = [_anchors_to_source(operation, document_id)]
    if existing is not None:
        parsed_sources = _merge_sources(existing.frontmatter.sources, parsed_sources)

    aliases = _unique(_string_values(raw.get("aliases")))
    tags = _unique(_string_values(raw.get("tags")))
    if existing is not None:
        aliases = _unique([*existing.frontmatter.aliases, *aliases])
        tags = _unique([*existing.frontmatter.tags, *tags])
        if not bool(raw.get("replace_existing")) and existing.body.strip() not in body:
            body = existing.body.strip() + "\n\n## 增量补充\n\n" + body

    title = display_title(
        str(raw.get("title") or (existing.title if existing else "")),
        page_key=key,
        page_type=page_type,
        body=body,
        hints=[operation.reason, *_string_values(raw.get("title_hints"))],
    )

    frontmatter = WikiFrontmatter(
        page_key=key,
        title=title,
        type=page_type,
        domain=raw.get("domain") if raw.get("domain") is not None else (
            existing.frontmatter.domain if existing else None
        ),
        aliases=aliases,
        tags=tags,
        sources=parsed_sources,
        status=str(
            raw.get("status")
            or (existing.frontmatter.status if existing else "published")
        ),
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
    page_type = existing.type if existing else (operation.page_type or "rule")
    title = display_title(
        existing.title if existing else "",
        page_key=operation.page_key,
        page_type=page_type,
        hints=[operation.reason, *selected],
    )
    body = "\n".join([f"# {title}", "", *[f"- {item}" for item in selected]])
    if not selected:
        body += "\n- 本来源未提取到可自动发布的规则，请人工复核。"
    return {
        "page_key": operation.page_key,
        "title": title,
        "type": page_type,
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
    title = display_title(
        plan.source_summary.title,
        page_key=key,
        page_type="source",
        hints=[plan.source_summary.summary, *(claim.statement for claim in plan.claims[:3])],
    )
    lines = [f"# {title}", ""]
    if plan.source_summary.summary:
        lines.extend([plan.source_summary.summary, ""])
    if plan.claims:
        lines.append("## 关键结论")
        lines.extend(f"- {claim.statement}" for claim in plan.claims[:40])
    return operation, {
        "page_key": key,
        "title": title,
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


def _sanitize_wikilinks(body: str, known_keys: set[str]) -> tuple[str, list[str]]:
    warnings: list[str] = []

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        raw_key = match.group(1).strip()
        display = raw[2:-2].split("|", 1)[1].strip() if "|" in raw[2:-2] else raw_key
        try:
            key = validate_page_key(raw_key)
        except ValueError:
            warnings.append(f"非法 Wiki 链接 {raw_key!r} 已转换为普通文本")
            return display
        if key not in known_keys:
            warnings.append(f"未知 Wiki 链接 {key} 已转换为普通文本")
            return display
        return raw

    return _WIKILINK_RE.sub(replace, str(body or "")), warnings


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
    candidate["sources"] = [
        source.model_dump(mode="json")
        for source in _merge_sources(
            existing.frontmatter.sources,
            [_anchors_to_source(operation, document_id)],
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
    space_id: int | None = None,
) -> int:
    item = WikiReviewItem(
        page_id=page_id,
        job_id=job_id,
        space_id=space_id,
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
    space_id: int | None = None,
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
        space_id=space_id,
    )


def apply_wiki_plan(
    session: Session,
    plan: StepAPlan | Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    *,
    document_id: int,
    job_id: int | None = None,
    max_pages: int = MAX_APPLY_PAGES,
    space_id: int | None = None,
) -> WikiApplyResult:
    """Apply each safe page independently and isolate model-level failures."""

    repository = WikiRepository(session, space_id=space_id) if space_id is not None else WikiRepository(session)
    existing_rows = repository.list_rows(include_archived=True)
    existing_keys = {row.page_key for row in existing_rows if row.page_key}
    parsed_plan = coerce_step_a_plan(
        plan,
        existing_page_keys=existing_keys,
        max_operations=max_pages,
    )
    result = WikiApplyResult()
    candidate_list = list(candidates)
    if len(candidate_list) > max_pages:
        result.warnings.append(
            f"Step B 返回 {len(candidate_list)} 个候选，已裁剪为 {max_pages} 个"
        )
        candidate_list = candidate_list[:max_pages]
    by_key: dict[str, Mapping[str, Any]] = {}
    for position, candidate in enumerate(candidate_list, start=1):
        try:
            key = validate_page_key(str(candidate.get("page_key") or ""))
        except ValueError:
            result.warnings.append(f"Step B 候选 {position} page_key 无效，已忽略")
            continue
        if key in by_key:
            result.warnings.append(f"Step B 重复候选 {key} 已忽略")
            continue
        by_key[key] = candidate

    operations = list(parsed_plan.page_operations)
    planned_keys = {operation.page_key for operation in operations if operation.op != "noop"}
    unexpected_keys = set(by_key) - planned_keys
    if unexpected_keys:
        result.warnings.append(
            "Step B 计划外页面已忽略：" + ", ".join(sorted(unexpected_keys))
        )
        by_key = {key: value for key, value in by_key.items() if key in planned_keys}

    summary_operation, summary_candidate = build_source_summary_candidate(parsed_plan, document_id)
    summary_key = summary_operation.page_key
    result.source_summary_key = summary_key
    if summary_key in existing_keys:
        summary_operation.op = "update"
    existing_summary_index = next(
        (index for index, item in enumerate(operations) if item.page_key == summary_key),
        None,
    )
    if existing_summary_index is None:
        operations.insert(0, summary_operation)
    else:
        operations.pop(existing_summary_index)
        operations.insert(0, summary_operation)
    by_key.setdefault(summary_key, summary_candidate)

    if len(operations) > max_pages:
        result.warnings.append(
            f"Wiki 操作共 {len(operations)} 条，已保留来源摘要和其余 {max_pages - 1} 条"
        )
        operations = [operations[0], *operations[1:max_pages]]
    known_keys = existing_keys | {item.page_key for item in operations}
    row_ids = {row.page_key: row.id for row in existing_rows if row.page_key}
    seen_review_keys: set[str] = set()
    for item in parsed_plan.review_items[:20]:
        dedupe_key = f"{item.kind}|{item.page_key}|{item.reason}"
        if dedupe_key in seen_review_keys:
            continue
        seen_review_keys.add(dedupe_key)
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
            space_id=repository.space_id,
        )
        result.review_item_ids.append(review_id)
    operation_keys = {operation.page_key for operation in operations}
    for contradiction in parsed_plan.contradictions[:20]:
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
            space_id=repository.space_id,
        )
        result.review_item_ids.append(review_id)
    for operation in operations:
        if operation.op == "noop":
            result.noop_page_keys.append(operation.page_key)
            continue
        live_exists = operation.page_key in existing_keys
        if live_exists and operation.op == "create":
            operation = operation.model_copy(update={"op": "update"})
            result.warnings.append(
                f"{operation.page_key} 已存在，写入阶段自动改为 update"
            )
        elif not live_exists and operation.op == "update":
            operation = operation.model_copy(update={"op": "create"})
            result.warnings.append(
                f"{operation.page_key} 不存在，写入阶段自动改为 create"
            )
        old_record = None
        old_page = None
        if operation.op == "update":
            try:
                old_record = repository.read(operation.page_key)
                old_page = old_record.page
            except WikiPageNotFoundError:
                operation = operation.model_copy(update={"op": "create"})
                result.warnings.append(
                    f"{operation.page_key} 在写入前消失，已改为 create"
                )
            except (WikiPageCorruptError, WikiPageFileError) as exc:
                fallback = _fallback_candidate(
                    parsed_plan,
                    operation,
                    document_id=document_id,
                )
                review_id = _queue_review(
                    session,
                    operation="update",
                    page_id=row_ids.get(operation.page_key),
                    page_key=operation.page_key,
                    job_id=job_id,
                    reason=f"现有页面不可读取，已隔离等待修复：{exc}",
                    risks=["existing_page_unreadable"],
                    candidate=fallback,
                    space_id=repository.space_id,
                )
                result.review_item_ids.append(review_id)
                result.skipped_page_keys.append(operation.page_key)
                result.warnings.append(
                    f"{operation.page_key} 现有页面不可读取，已转为审核项"
                )
                continue
            except WikiAtomicApplyError:
                raise

        raw = dict(by_key.get(operation.page_key) or {})
        if not raw:
            raw = _fallback_candidate(
                parsed_plan,
                operation,
                document_id=document_id,
                existing=old_page,
            )
            result.warnings.append(
                f"{operation.page_key} 缺少可用 Step B 正文，已使用确定性正文"
            )
        raw["title_hints"] = [
            claim.statement
            for claim in parsed_plan.claims
            if not operation.claim_ids or claim.claim_id in operation.claim_ids
        ][:3]
        sanitized_body, link_warnings = _sanitize_wikilinks(
            str(raw.get("body") or ""), known_keys
        )
        raw["body"] = sanitized_body
        result.warnings.extend(
            f"{operation.page_key}: {message}" for message in link_warnings
        )
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
                    space_id=repository.space_id,
                )
                result.review_item_ids.append(review_id)
                continue
        try:
            page = _candidate_page(
                raw,
                operation=operation,
                document_id=document_id,
                existing=old_page,
            )
        except ValueError as exc:
            fallback = _fallback_candidate(
                parsed_plan,
                operation,
                document_id=document_id,
                existing=old_page,
            )
            result.warnings.append(
                f"{operation.page_key} 候选字段不可用（{exc}），已使用确定性候选"
            )
            page = _candidate_page(
                fallback,
                operation=operation,
                document_id=document_id,
                existing=old_page,
            )
            raw = fallback
        reason = operation.reason or str(raw.get("reason") or operation.op)
        try:
            if operation.op == "create":
                repository.create(page, job_id=job_id, reason=reason)
            else:
                repository.update(operation.page_key, page, job_id=job_id, reason=reason)
        except WikiPageAlreadyExistsError:
            current = repository.read(operation.page_key)
            update_operation = operation.model_copy(update={"op": "update"})
            page = _candidate_page(
                raw,
                operation=update_operation,
                document_id=document_id,
                existing=current.page,
            )
            repository.update(operation.page_key, page, job_id=job_id, reason=reason)
            result.warnings.append(
                f"{operation.page_key} 并发创建冲突，已自动转为 update"
            )
        except (WikiPageCorruptError, WikiPageFileError, WikiAtomicApplyError):
            raise
        except (ValueError, WikiPageNotFoundError) as exc:
            review_id = _queue_review(
                session,
                operation="review",
                page_id=row_ids.get(operation.page_key),
                page_key=operation.page_key,
                job_id=job_id,
                reason=f"页面写入已隔离：{exc}",
                risks=["page_apply_failed"],
                candidate=raw,
                space_id=repository.space_id,
            )
            result.review_item_ids.append(review_id)
            result.skipped_page_keys.append(operation.page_key)
            result.warnings.append(
                f"{operation.page_key} 写入失败，已转为审核项并继续处理其他页面"
            )
            continue
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
