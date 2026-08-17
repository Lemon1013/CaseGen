"""Normalized test-point checkpoint and prompt parsing helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from sqlmodel import Session, col, func, select

from app.models.entities import (
    GenerationTask,
    TaskCitation,
    TaskRetrievalCheckpoint,
    TaskTestPointCheckpoint,
    TestPoint,
    TestPointCitation,
)

TEST_DIMENSIONS = (
    "positive",
    "negative",
    "boundary",
    "permission",
    "security",
    "compatibility",
    "performance",
    "recovery",
    "usability",
)
DEFAULT_TEST_DIMENSIONS = ("positive", "negative", "boundary")
PRIORITIES = ("P0", "P1", "P2")
_KEY_RE = re.compile(r"^TP-[A-Z0-9][A-Z0-9_.-]{0,79}$", re.IGNORECASE)


def task_dimensions(task: GenerationTask) -> list[str]:
    try:
        raw = json.loads(task.test_dimensions_json or "[]")
    except (TypeError, ValueError):
        raw = []
    if not isinstance(raw, list):
        raw = []
    values = [str(item).strip().lower() for item in raw if str(item).strip().lower() in TEST_DIMENSIONS]
    return list(dict.fromkeys(values)) or list(DEFAULT_TEST_DIMENSIONS)


def extract_json_payload(raw: str) -> Any:
    """Decode a JSON object/array from a tolerant model response."""

    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
            return value
        except json.JSONDecodeError:
            continue
    raise ValueError("模型未返回可解析的测试点 JSON")


def citation_label_map_from_context(citations: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Build the exact label map shown in the current model prompt.

    Labels are intentionally supplied by the caller from the selected,
    reassembled context.  A numeric label is therefore never guessed to be a
    database id.
    """

    mapping: dict[str, int] = {}
    for citation in citations:
        label = str(citation.get("label") or citation.get("citation_id") or "").strip()
        citation_id = citation.get("task_citation_id")
        if label and citation_id is not None:
            mapping[label.strip("[]").lower()] = int(citation_id)
    return mapping


def _citation_ids(
    value: Any,
    *,
    labels: Mapping[str, int],
) -> tuple[list[int], int]:
    if not isinstance(value, (list, tuple, set)):
        value = [value] if value not in (None, "") else []
    result: list[int] = []
    unknown = 0
    for item in value:
        raw = str(item).strip().strip("[]")
        if not raw:
            continue
        candidate: int | None = None
        # ``raw`` is a model-visible label such as ``1`` or ``S1``.  Only the
        # explicit prompt map is accepted; never compare it with DB ids.
        candidate = labels.get(raw.lower())
        if candidate is None:
            unknown += 1
            continue
        if candidate not in result:
            result.append(candidate)
    return result, unknown


def normalize_model_points(
    session: Session,
    task: GenerationTask,
    payload: Any,
    *,
    citation_label_map: Mapping[str, int],
) -> tuple[list[dict[str, Any]], int]:
    """Normalize model output and discard unknown citation references.

    The returned citation ids are database ids, never model-provided labels.
    ``unknown_citations`` is surfaced in a TaskEvent by the caller for
    diagnostics rather than being allowed to become a false relationship.
    """

    if isinstance(payload, Mapping):
        raw_points = payload.get("test_points") or payload.get("points") or payload.get("items") or []
    else:
        raw_points = payload
    if not isinstance(raw_points, list):
        raise ValueError("测试点 JSON 的 test_points 必须是数组")

    dimensions = task_dimensions(task)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    unknown_total = 0
    for index, raw in enumerate(raw_points, start=1):
        if not isinstance(raw, Mapping):
            continue
        key = str(raw.get("stable_key") or raw.get("key") or f"TP-{index:03d}").strip().upper()
        if not key.startswith("TP-"):
            key = f"TP-{key}"
        if not _KEY_RE.fullmatch(key) or key in seen:
            continue
        title = str(raw.get("title") or raw.get("name") or "").strip()
        goal = str(raw.get("verification_goal") or raw.get("verify") or raw.get("goal") or "").strip()
        if not title or not goal:
            continue
        dimension = str(raw.get("dimension") or dimensions[0]).strip().lower()
        if dimension not in dimensions:
            dimension = dimensions[0]
        priority = str(raw.get("priority") or "P1").strip().upper()
        if priority not in PRIORITIES:
            priority = "P1"
        citation_ids, unknown = _citation_ids(
            raw.get("citation_ids") or raw.get("citations") or [],
            labels=citation_label_map,
        )
        unknown_total += unknown
        seen.add(key)
        normalized.append(
            {
                "stable_key": key,
                "title": title[:240],
                "verification_goal": goal[:1000],
                "dimension": dimension,
                "priority": priority,
                "sort_order": index - 1,
                "is_selected": True,
                "is_excluded": False,
                "citation_ids": citation_ids,
            }
        )
    if not normalized:
        raise ValueError("模型未返回有效测试点")
    return normalized, unknown_total


def latest_checkpoint(session: Session, task_id: int) -> TaskTestPointCheckpoint | None:
    return session.exec(
        select(TaskTestPointCheckpoint)
        .where(TaskTestPointCheckpoint.task_id == task_id)
        .order_by(col(TaskTestPointCheckpoint.attempt).desc())
    ).first()


def current_points(session: Session, task_id: int) -> list[TestPoint]:
    checkpoint = latest_checkpoint(session, task_id)
    if checkpoint is None:
        return []
    return list(
        session.exec(
            select(TestPoint)
            .where(TestPoint.checkpoint_id == checkpoint.id)
            .order_by(col(TestPoint.sort_order).asc(), col(TestPoint.id).asc())
        ).all()
    )


def create_checkpoint(
    session: Session,
    task: GenerationTask,
    retrieval_checkpoint: TaskRetrievalCheckpoint,
    points: Iterable[Mapping[str, Any]],
) -> TaskTestPointCheckpoint:
    attempt = (
        session.exec(
            select(func.max(TaskTestPointCheckpoint.attempt)).where(
                TaskTestPointCheckpoint.task_id == task.id
            )
        ).one()
        or 0
    ) + 1
    checkpoint = TaskTestPointCheckpoint(
        task_id=int(task.id),
        retrieval_checkpoint_id=int(retrieval_checkpoint.id),
        attempt=int(attempt),
        version=1,
        status="pending",
        auto_review=bool(retrieval_checkpoint.auto_review),
    )
    session.add(checkpoint)
    session.flush()
    write_points(session, task, checkpoint, points)
    return checkpoint


def validate_point_inputs(
    session: Session,
    task: GenerationTask,
    points: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    allowed_dimensions = set(task_dimensions(task))
    valid_citations = {
        int(row.id)
        for row in session.exec(select(TaskCitation).where(TaskCitation.task_id == task.id)).all()
        if row.id is not None
    }
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(points):
        if not isinstance(raw, Mapping):
            raise ValueError("测试点必须是对象")
        key = str(raw.get("stable_key") or "").strip().upper()
        if not _KEY_RE.fullmatch(key):
            raise ValueError(f"测试点稳定 key 无效：{key or '空'}")
        if key in seen:
            raise ValueError(f"测试点稳定 key 重复：{key}")
        seen.add(key)
        title = str(raw.get("title") or "").strip()
        goal = str(raw.get("verification_goal") or "").strip()
        dimension = str(raw.get("dimension") or "").strip().lower()
        priority = str(raw.get("priority") or "").strip().upper()
        if not title or not goal:
            raise ValueError(f"测试点 {key} 的标题和验证目标不能为空")
        if dimension not in allowed_dimensions:
            raise ValueError(f"测试点 {key} 的维度不在任务配置中")
        if priority not in PRIORITIES:
            raise ValueError(f"测试点 {key} 的优先级必须是 P0/P1/P2")
        citation_ids = [int(item) for item in (raw.get("citation_ids") or [])]
        if any(item not in valid_citations for item in citation_ids):
            raise ValueError(f"测试点 {key} 引用了不属于当前任务的 citation")
        normalized.append(
            {
                "stable_key": key,
                "title": title[:240],
                "verification_goal": goal[:1000],
                "dimension": dimension,
                "priority": priority,
                "sort_order": index,
                "is_selected": bool(raw.get("is_selected", True)),
                "is_excluded": bool(raw.get("is_excluded", False)),
                "citation_ids": list(dict.fromkeys(citation_ids)),
            }
        )
    if not normalized:
        raise ValueError("至少保留一个测试点")
    if not any(item["is_selected"] and not item["is_excluded"] for item in normalized):
        raise ValueError("至少选择一个未排除的测试点")
    return normalized


def write_points(
    session: Session,
    task: GenerationTask,
    checkpoint: TaskTestPointCheckpoint,
    points: Iterable[Mapping[str, Any]],
) -> list[TestPoint]:
    existing = session.exec(select(TestPoint).where(TestPoint.checkpoint_id == checkpoint.id)).all()
    for row in existing:
        links = session.exec(select(TestPointCitation).where(TestPointCitation.test_point_id == row.id)).all()
        for link in links:
            session.delete(link)
        session.delete(row)
    session.flush()
    rows: list[TestPoint] = []
    for item in points:
        row = TestPoint(
            task_id=int(task.id),
            checkpoint_id=int(checkpoint.id),
            stable_key=str(item["stable_key"]),
            title=str(item["title"]),
            verification_goal=str(item["verification_goal"]),
            dimension=str(item["dimension"]),
            priority=str(item["priority"]),
            sort_order=int(item.get("sort_order", len(rows))),
            is_selected=bool(item.get("is_selected", True)),
            is_excluded=bool(item.get("is_excluded", False)),
        )
        session.add(row)
        session.flush()
        for citation_id in item.get("citation_ids") or []:
            session.add(TestPointCitation(test_point_id=int(row.id), citation_id=int(citation_id)))
        rows.append(row)
    session.flush()
    return rows


def point_citation_ids(session: Session, point_id: int) -> list[int]:
    return [
        int(row.citation_id)
        for row in session.exec(
            select(TestPointCitation)
            .where(TestPointCitation.test_point_id == point_id)
            .order_by(col(TestPointCitation.citation_id).asc())
        ).all()
    ]


def points_for_prompt(session: Session, task_id: int) -> list[TestPoint]:
    return [row for row in current_points(session, task_id) if row.is_selected and not row.is_excluded]
