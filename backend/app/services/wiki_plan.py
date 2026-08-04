"""Strict Step A contracts and merge helpers for Wiki 2.0.

This module validates model output before any later page-writing task sees it.
It intentionally stops at a change plan: only create, update and noop are
accepted here; merge is reserved for a reviewed future task.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.services.wiki_schema import is_valid_page_key, validate_page_key


class PlanValidationError(ValueError):
    """Raised when Step A output cannot be safely used as a change plan."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Mapping):
        values = [value]
    else:
        try:
            values = list(value)
        except TypeError:
            values = [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, Mapping):
            item = item.get("name") or item.get("title") or item.get("text") or item
        text = str(item).strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _positive_ints(value: Any) -> list[int]:
    if value is None:
        return []
    values = [value] if isinstance(value, (str, int)) else list(value)
    result: list[int] = []
    for item in values:
        if isinstance(item, bool):
            raise ValueError("anchor ids must be positive integers")
        number = int(item)
        if number < 1:
            raise ValueError("anchor ids must be positive integers")
        if number not in result:
            result.append(number)
    return result


class SourceAnchor(_StrictModel):
    """A source locator emitted by Step A.

    At least one concrete locator is required.  ``window_index`` is valid for
    the long-analyze intermediate result; chunk/page/clause/range locators are
    preferred once parsed source metadata is available.
    """

    document_id: int | None = Field(default=None, ge=1)
    source_path: str | None = None
    chunk_id: int | None = Field(default=None, ge=1, validation_alias=AliasChoices("chunk_id"))
    chunk_ids: list[int] = Field(default_factory=list)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    section: str | None = None
    clause_id: str | None = None
    clause_ids: list[str] = Field(default_factory=list)
    window_index: int | None = Field(default=None, ge=1)
    start_char: int | None = Field(default=None, ge=0, validation_alias=AliasChoices("start_char", "start"))
    end_char: int | None = Field(default=None, ge=0, validation_alias=AliasChoices("end_char", "end"))

    @field_validator("chunk_ids", mode="before")
    @classmethod
    def _chunk_ids(cls, value: Any) -> list[int]:
        return _positive_ints(value)

    @field_validator("clause_ids", mode="before")
    @classmethod
    def _clause_ids(cls, value: Any) -> list[str]:
        return _strings(value)

    @field_validator("source_path", "section", "clause_id")
    @classmethod
    def _optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value else None

    @model_validator(mode="after")
    def _valid_locator(self) -> "SourceAnchor":
        if self.chunk_id is not None and self.chunk_id not in self.chunk_ids:
            self.chunk_ids.insert(0, self.chunk_id)
        if self.clause_id and self.clause_id not in self.clause_ids:
            self.clause_ids.insert(0, self.clause_id)
        if self.page_start is not None and self.page_end is not None and self.page_end < self.page_start:
            raise ValueError("page_end must be greater than or equal to page_start")
        if self.start_char is not None and self.end_char is not None and self.end_char <= self.start_char:
            raise ValueError("end_char must be greater than start_char")
        if not any(
            (
                self.chunk_id,
                self.chunk_ids,
                self.page_start,
                self.page_end,
                self.section,
                self.clause_id,
                self.clause_ids,
                self.window_index,
                self.start_char is not None,
                self.end_char is not None,
            )
        ):
            raise ValueError("source anchor must contain a concrete locator")
        return self


class SourceSummary(_StrictModel):
    title: str = ""
    summary: str = ""
    source_path: str | None = None
    filename: str | None = None
    domain: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, value: Any) -> list[str]:
        return _strings(value)


class Claim(_StrictModel):
    claim_id: str = Field(default="", validation_alias=AliasChoices("claim_id", "id"))
    statement: str = Field(min_length=1, validation_alias=AliasChoices("statement", "text", "claim", "rule"))
    kind: str = "rule"
    entities: list[str] = Field(default_factory=list, validation_alias=AliasChoices("entities", "entity_keys"))
    clauses: list[str] = Field(default_factory=list, validation_alias=AliasChoices("clauses", "clause_ids"))
    source_anchors: list[SourceAnchor] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("entities", "clauses", mode="before")
    @classmethod
    def _lists(cls, value: Any) -> list[str]:
        return _strings(value)


class RelatedPage(_StrictModel):
    page_key: str
    relation: str = "related"
    reason: str = ""
    matched_on: list[str] = Field(default_factory=list)
    score: float | None = None

    @field_validator("page_key")
    @classmethod
    def _key(cls, value: str) -> str:
        return validate_page_key(value)

    @field_validator("matched_on", mode="before")
    @classmethod
    def _matched_on(cls, value: Any) -> list[str]:
        return _strings(value)


class Contradiction(_StrictModel):
    page_key: str | None = None
    description: str = Field(min_length=1, validation_alias=AliasChoices("description", "reason", "text"))
    claim_ids: list[str] = Field(default_factory=list)
    source_anchors: list[SourceAnchor] = Field(default_factory=list)

    @field_validator("page_key")
    @classmethod
    def _key(cls, value: str | None) -> str | None:
        return validate_page_key(value) if value else None

    @field_validator("claim_ids", mode="before")
    @classmethod
    def _claim_ids(cls, value: Any) -> list[str]:
        return _strings(value)


class PageOperation(_StrictModel):
    op: Literal["create", "update", "noop"]
    page_key: str
    reason: str = ""
    source_anchors: list[SourceAnchor] = Field(default_factory=list)
    page_type: Literal["source", "rule", "entity", "scenario", "regression", "synthesis"] | None = None
    claim_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("page_key")
    @classmethod
    def _key(cls, value: str) -> str:
        return validate_page_key(value)

    @field_validator("claim_ids", mode="before")
    @classmethod
    def _claim_ids(cls, value: Any) -> list[str]:
        return _strings(value)


class ReviewItem(_StrictModel):
    kind: str = "needs_review"
    reason: str = Field(min_length=1)
    page_key: str | None = None
    claim_ids: list[str] = Field(default_factory=list)
    source_anchors: list[SourceAnchor] = Field(default_factory=list)
    severity: Literal["low", "medium", "high"] = "medium"

    @field_validator("page_key")
    @classmethod
    def _key(cls, value: str | None) -> str | None:
        return validate_page_key(value) if value else None

    @field_validator("claim_ids", mode="before")
    @classmethod
    def _claim_ids(cls, value: Any) -> list[str]:
        return _strings(value)


class StepAPlan(_StrictModel):
    source_summary: SourceSummary = Field(default_factory=SourceSummary)
    claims: list[Claim] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    related_pages: list[RelatedPage] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    page_operations: list[PageOperation] = Field(default_factory=list)
    review_items: list[ReviewItem] = Field(default_factory=list)

    @field_validator("entities", mode="before")
    @classmethod
    def _entities(cls, value: Any) -> list[str]:
        return _strings(value)


def _window_list(source_windows: Iterable[Any] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in source_windows or ():
        if isinstance(raw, Mapping):
            item = dict(raw)
        else:
            item = {name: getattr(raw, name) for name in ("index", "start", "end", "page_start", "page_end", "section", "clause_ids", "chunk_ids", "document_id", "source_path") if hasattr(raw, name)}
        result.append(item)
    return result


def validate_source_anchor(
    anchor: SourceAnchor | Mapping[str, Any],
    *,
    source_windows: Iterable[Any] | None = None,
    source_length: int | None = None,
) -> SourceAnchor:
    """Validate structure and, when available, membership in parsed windows."""

    try:
        parsed = anchor if isinstance(anchor, SourceAnchor) else SourceAnchor.model_validate(anchor)
    except (ValidationError, TypeError, ValueError) as exc:
        raise PlanValidationError(f"invalid source anchor: {exc}") from exc
    windows = _window_list(source_windows)
    if source_length is not None and parsed.end_char is not None and parsed.end_char > source_length:
        raise PlanValidationError("source anchor end_char exceeds source length")
    if parsed.start_char is not None and parsed.end_char is None:
        raise PlanValidationError("source anchor with start_char must include end_char")
    if parsed.window_index is not None and windows:
        if not any(int(item.get("index", item.get("window_index", 0)) or 0) == parsed.window_index for item in windows):
            raise PlanValidationError(f"source window does not exist: {parsed.window_index}")
    if not windows:
        return parsed

    def known_values(name: str) -> set[str]:
        values: set[str] = set()
        for item in windows:
            raw = item.get(name)
            if isinstance(raw, (list, tuple, set)):
                values.update(str(value) for value in raw)
            elif raw is not None:
                values.add(str(raw))
        return values

    known_chunks = known_values("chunk_ids") | known_values("chunk_id")
    requested_chunks = {str(value) for value in parsed.chunk_ids}
    if parsed.chunk_id is not None:
        requested_chunks.add(str(parsed.chunk_id))
    if requested_chunks and (not known_chunks or not requested_chunks.issubset(known_chunks)):
        raise PlanValidationError("source anchor chunk is not present in source windows")
    known_clauses = known_values("clause_ids") | known_values("clauses") | known_values("clause_id")
    requested_clauses = set(parsed.clause_ids)
    if parsed.clause_id:
        requested_clauses.add(parsed.clause_id)
    if requested_clauses and (not known_clauses or not requested_clauses.issubset(known_clauses)):
        raise PlanValidationError("source anchor clause is not present in source windows")

    known_sections = known_values("section")
    if parsed.section and (not known_sections or parsed.section not in known_sections):
        raise PlanValidationError("source anchor section is not present in source windows")

    known_documents = known_values("document_id")
    if parsed.document_id is not None and known_documents and str(parsed.document_id) not in known_documents:
        raise PlanValidationError("source anchor document does not match source windows")
    known_paths = known_values("source_path")
    if parsed.source_path and known_paths and parsed.source_path not in known_paths:
        raise PlanValidationError("source anchor path does not match source windows")

    if parsed.page_start is not None or parsed.page_end is not None:
        requested_start = parsed.page_start or parsed.page_end
        requested_end = parsed.page_end or parsed.page_start
        page_ranges = [
            (
                int(item.get("page_start") or item.get("page_end") or 0),
                int(item.get("page_end") or item.get("page_start") or 0),
            )
            for item in windows
            if item.get("page_start") is not None or item.get("page_end") is not None
        ]
        if not page_ranges or not any(
            max(start, int(requested_start or 0)) <= min(end, int(requested_end or 0))
            for start, end in page_ranges
        ):
            raise PlanValidationError("source anchor page is not present in source windows")

    if parsed.start_char is not None and parsed.end_char is not None:
        overlaps = False
        for item in windows:
            start = int(item.get("start", item.get("start_char", 0)) or 0)
            end = int(item.get("end", item.get("end_char", 0)) or 0)
            if max(start, parsed.start_char) < min(end, parsed.end_char):
                overlaps = True
                break
        if not overlaps and any("start" in item or "start_char" in item for item in windows):
            raise PlanValidationError("source anchor range does not overlap a source window")
    return parsed


def validate_step_a_plan(
    value: StepAPlan | Mapping[str, Any],
    *,
    existing_page_keys: Iterable[str] | None = None,
    source_windows: Iterable[Any] | None = None,
    source_length: int | None = None,
    max_operations: int = 64,
) -> StepAPlan:
    """Strictly validate a Step A plan and its references."""

    try:
        plan = value if isinstance(value, StepAPlan) else StepAPlan.model_validate(value)
    except (ValidationError, TypeError, ValueError) as exc:
        raise PlanValidationError(f"invalid Step A plan: {exc}") from exc
    if len(plan.page_operations) > max_operations:
        raise PlanValidationError(f"too many page operations: {len(plan.page_operations)}")
    known_keys: set[str] | None = None
    if existing_page_keys is not None:
        known_keys = set()
        for key in existing_page_keys:
            if not is_valid_page_key(str(key)):
                raise PlanValidationError(f"invalid existing page_key: {key}")
            known_keys.add(str(key))

    def check_key(key: str | None, label: str) -> None:
        if key and not is_valid_page_key(key):
            raise PlanValidationError(f"invalid {label} page_key: {key}")
        if key and known_keys is not None and label in {"related", "contradiction", "review"} and key not in known_keys:
            raise PlanValidationError(f"{label} page_key does not exist: {key}")

    seen_ops: set[str] = set()
    for operation in plan.page_operations:
        check_key(operation.page_key, "operation")
        if operation.page_key in seen_ops:
            raise PlanValidationError(f"duplicate page operation: {operation.page_key}")
        seen_ops.add(operation.page_key)
        if known_keys is not None:
            if operation.op in {"update", "noop"} and operation.page_key not in known_keys:
                raise PlanValidationError(f"{operation.op} target page does not exist: {operation.page_key}")
            if operation.op == "create" and operation.page_key in known_keys:
                raise PlanValidationError(f"create target page already exists: {operation.page_key}")
        for anchor in operation.source_anchors:
            validate_source_anchor(anchor, source_windows=source_windows, source_length=source_length)
        if operation.op in {"create", "update"} and not operation.source_anchors:
            raise PlanValidationError(f"{operation.op} requires at least one source anchor: {operation.page_key}")

    for claim in plan.claims:
        for anchor in claim.source_anchors:
            validate_source_anchor(anchor, source_windows=source_windows, source_length=source_length)
    for item in plan.related_pages:
        check_key(item.page_key, "related")
    for item in plan.contradictions:
        check_key(item.page_key, "contradiction")
        for anchor in item.source_anchors:
            validate_source_anchor(anchor, source_windows=source_windows, source_length=source_length)
    for item in plan.review_items:
        check_key(item.page_key, "review")
        for anchor in item.source_anchors:
            validate_source_anchor(anchor, source_windows=source_windows, source_length=source_length)
    return plan


def _legacy_claims(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    claims = raw.get("claims")
    if claims is not None:
        return list(claims) if isinstance(claims, list) else [claims]
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw.get("key_rules") or [], start=1):
        result.append({"claim_id": f"claim-{index}", "statement": str(item)})
    return result


def coerce_step_a_plan(
    raw: StepAPlan | Mapping[str, Any],
    *,
    source_path: str = "",
    source_windows: Iterable[Any] | None = None,
    existing_page_keys: Iterable[str] | None = None,
    source_length: int | None = None,
) -> StepAPlan:
    """Accept the pre-Task-6 analysis JSON while producing strict Step A."""

    if isinstance(raw, StepAPlan):
        return validate_step_a_plan(
            raw,
            existing_page_keys=existing_page_keys,
            source_windows=source_windows,
            source_length=source_length,
        )
    if not isinstance(raw, Mapping):
        raise PlanValidationError("Step A output must be a JSON object")
    data = dict(raw)
    is_new_shape = any(key in data for key in ("source_summary", "claims", "related_pages", "contradictions", "page_operations", "review_items"))
    if not is_new_shape:
        data = {
            "source_summary": {
                "title": str(raw.get("summary_title") or ""),
                "summary": str(raw.get("global_digest") or ""),
                "source_path": source_path or None,
            },
            "claims": _legacy_claims(raw),
            "entities": _strings(raw.get("entities")),
            "related_pages": [],
            "contradictions": [],
            "page_operations": [],
            "review_items": [],
        }
    else:
        allowed = {"source_summary", "claims", "entities", "related_pages", "contradictions", "page_operations", "review_items"}
        legacy_compat = {
            "summary_title",
            "key_rules",
            "api_points",
            "test_hints",
            "suggested_page_types",
            "global_digest",
            "digest_update",
        }
        unknown = set(data) - allowed - legacy_compat
        if unknown:
            raise PlanValidationError(
                "unknown Step A fields: " + ", ".join(sorted(unknown))
            )
        data = {key: value for key, value in data.items() if key in allowed}
        data.setdefault("source_summary", {})
        data.setdefault("claims", [])
        data.setdefault("entities", [])
        data.setdefault("related_pages", [])
        data.setdefault("contradictions", [])
        data.setdefault("page_operations", [])
        data.setdefault("review_items", [])
    try:
        plan = StepAPlan.model_validate(data)
    except (ValidationError, TypeError, ValueError) as exc:
        raise PlanValidationError(f"invalid Step A plan: {exc}") from exc
    return validate_step_a_plan(
        plan,
        existing_page_keys=existing_page_keys,
        source_windows=source_windows,
        source_length=source_length,
    )


def _normalise_key(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def _bounded(items: list[Any], maximum: int) -> list[Any]:
    if maximum <= 0 or len(items) <= maximum:
        return items
    head = max(1, maximum // 2)
    return items[:head] + items[-(maximum - head):]


def _dedupe_strings(values: Iterable[str], maximum: int = 80) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        key = _normalise_key(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return _bounded(result, maximum)


def _merge_claims(plans: list[StepAPlan], maximum: int) -> list[Claim]:
    result: list[Claim] = []
    by_key: dict[str, Claim] = {}
    for plan in plans:
        for claim in plan.claims:
            semantic = _normalise_key(claim.statement)
            if claim.clauses:
                semantic += "|" + "|".join(sorted(_normalise_key(item) for item in claim.clauses))
            if semantic in by_key:
                old = by_key[semantic]
                old.source_anchors.extend(claim.source_anchors)
                old.source_anchors = _unique_anchors(old.source_anchors)
                old.entities = _dedupe_strings([*old.entities, *claim.entities], maximum=40)
                old.clauses = _dedupe_strings([*old.clauses, *claim.clauses], maximum=40)
                continue
            copied = claim.model_copy(deep=True)
            by_key[semantic] = copied
            result.append(copied)
    return _bounded(result, maximum)


def _unique_anchors(anchors: Iterable[SourceAnchor]) -> list[SourceAnchor]:
    result: list[SourceAnchor] = []
    seen: set[str] = set()
    for anchor in anchors:
        key = json.dumps(anchor.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(anchor)
    return result


def merge_step_a_plans(
    plans: Iterable[StepAPlan | Mapping[str, Any]],
    *,
    source_windows: Iterable[Any] | None = None,
    existing_page_keys: Iterable[str] | None = None,
    source_length: int | None = None,
    max_claims: int = 80,
    max_related_pages: int = 80,
    max_operations: int = 64,
) -> StepAPlan:
    """Merge all windows, preserving a tail quota after semantic dedupe."""

    parsed = [
        coerce_step_a_plan(
            item,
            source_windows=source_windows,
            existing_page_keys=existing_page_keys,
            source_length=source_length,
        )
        for item in plans
    ]
    if not parsed:
        return StepAPlan()
    summary = next((item.source_summary for item in parsed if item.source_summary.title or item.source_summary.summary), SourceSummary())
    entities = _dedupe_strings([entity for plan in parsed for entity in plan.entities], maximum=80)

    related: list[RelatedPage] = []
    related_seen: set[str] = set()
    for plan in parsed:
        for item in plan.related_pages:
            if item.page_key in related_seen:
                continue
            related_seen.add(item.page_key)
            related.append(item.model_copy(deep=True))
    related = _bounded(related, max_related_pages)

    contradictions: list[Contradiction] = []
    contradiction_seen: set[str] = set()
    reviews: list[ReviewItem] = []
    review_seen: set[str] = set()
    for plan in parsed:
        for item in plan.contradictions:
            key = f"{item.page_key}|{_normalise_key(item.description)}"
            if key not in contradiction_seen:
                contradiction_seen.add(key)
                contradictions.append(item.model_copy(deep=True))
        for item in plan.review_items:
            key = f"{item.page_key}|{item.kind}|{_normalise_key(item.reason)}"
            if key not in review_seen:
                review_seen.add(key)
                reviews.append(item.model_copy(deep=True))

    operations: list[PageOperation] = []
    operation_index: dict[str, int] = {}
    priority = {"noop": 0, "create": 1, "update": 2}
    for plan in parsed:
        for item in plan.page_operations:
            if item.page_key not in operation_index:
                operation_index[item.page_key] = len(operations)
                operations.append(item.model_copy(deep=True))
                continue
            index = operation_index[item.page_key]
            old = operations[index]
            chosen_op = old.op if priority[old.op] >= priority[item.op] else item.op
            operations[index] = old.model_copy(
                update={
                    "op": chosen_op,
                    "reason": old.reason or item.reason,
                    "source_anchors": _unique_anchors([*old.source_anchors, *item.source_anchors]),
                    "claim_ids": _dedupe_strings([*old.claim_ids, *item.claim_ids], maximum=40),
                    "confidence": max(
                        [value for value in (old.confidence, item.confidence) if value is not None],
                        default=None,
                    ),
                }
            )

    merged = StepAPlan(
        source_summary=summary,
        claims=_merge_claims(parsed, max_claims),
        entities=entities,
        related_pages=related,
        contradictions=_bounded(contradictions, 80),
        page_operations=operations,
        review_items=_bounded(reviews, 80),
    )
    return validate_step_a_plan(
        merged,
        existing_page_keys=existing_page_keys,
        source_windows=source_windows,
        source_length=source_length,
        max_operations=max_operations,
    )


def plan_to_legacy_analysis(plan: StepAPlan, raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Expose old summary fields so the existing wiki writer remains usable."""

    raw = raw or {}
    return {
        "summary_title": plan.source_summary.title or str(raw.get("summary_title") or ""),
        "key_rules": [claim.statement for claim in plan.claims],
        "api_points": _strings(raw.get("api_points")),
        "test_hints": _strings(raw.get("test_hints")),
        "entities": plan.entities,
        "suggested_page_types": _strings(raw.get("suggested_page_types")) or ["source_summary"],
        "global_digest": plan.source_summary.summary or str(raw.get("global_digest") or ""),
        "claims": [claim.model_dump(mode="json") for claim in plan.claims],
        "related_pages": [item.model_dump(mode="json") for item in plan.related_pages],
        "contradictions": [item.model_dump(mode="json") for item in plan.contradictions],
        "page_operations": [item.model_dump(mode="json") for item in plan.page_operations],
        "review_items": [item.model_dump(mode="json") for item in plan.review_items],
    }


__all__ = [
    "Claim",
    "Contradiction",
    "PageOperation",
    "PlanValidationError",
    "RelatedPage",
    "ReviewItem",
    "SourceAnchor",
    "SourceSummary",
    "StepAPlan",
    "coerce_step_a_plan",
    "merge_step_a_plans",
    "plan_to_legacy_analysis",
    "validate_source_anchor",
    "validate_step_a_plan",
]
