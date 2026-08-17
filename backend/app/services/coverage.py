"""Deterministic task coverage calculations from normalized relations."""

from __future__ import annotations

from sqlmodel import Session, col, select

from app.models.entities import (
    CaseDraft,
    GenerationTask,
    TaskCitation,
    TestPoint,
    TestPointCaseLink,
    TestPointCitation,
)
from app.services.case_management import CaseDraftParseError, split_case_draft
from app.services.test_points import latest_checkpoint


_DRAFT_COVERAGE_STATUSES = {"generated", "reviewing", "reviewed", "optimizing"}


def _draft_cases_by_point(
    session: Session,
    task: GenerationTask | None,
    checkpoint_status: str,
    points: list[TestPoint],
) -> dict[int, list[str]]:
    """Derive preview coverage from the latest draft without persisting case links.

    Draft coverage is only meaningful after complete-case generation has
    committed for the current confirmed checkpoint.  During a regenerate or
    either human checkpoint, an older draft must not be matched to new points
    that happen to reuse the same stable key.
    """

    if (
        task is None
        or task.status not in _DRAFT_COVERAGE_STATUSES
        or checkpoint_status != "confirmed"
    ):
        return {}
    draft = session.exec(
        select(CaseDraft)
        .where(CaseDraft.task_id == task.id)
        .order_by(col(CaseDraft.version).desc(), col(CaseDraft.id).desc())
    ).first()
    if draft is None:
        return {}
    try:
        sections = split_case_draft(draft.content_md)
    except CaseDraftParseError:
        return {}

    current_by_key = {
        row.stable_key.upper(): row
        for row in points
        if row.id is not None and row.stable_key
    }
    draft_cases_by_point: dict[int, list[str]] = {}
    for section in sections:
        case_key = str(section.get("case_key") or "").upper()
        for stable_key in section.get("test_point_keys") or []:
            point = current_by_key.get(str(stable_key).upper())
            if point is None:
                continue
            case_keys = draft_cases_by_point.setdefault(int(point.id), [])
            if case_key and case_key not in case_keys:
                case_keys.append(case_key)
    return draft_cases_by_point


def build_coverage(session: Session, task_id: int) -> dict:
    task = session.get(GenerationTask, task_id)
    checkpoint = latest_checkpoint(session, task_id)
    if checkpoint is None:
        return {
            "task_id": task_id,
            "total_test_points": 0,
            "selected_test_points": 0,
            "covered_test_points": 0,
            "uncovered_test_points": 0,
            "coverage_percent": 0.0,
            "points": [],
            "citations": [],
        }

    points = session.exec(
        select(TestPoint)
        .where(TestPoint.checkpoint_id == checkpoint.id)
        .order_by(col(TestPoint.sort_order).asc(), col(TestPoint.id).asc())
    ).all()
    point_ids = [int(row.id) for row in points if row.id is not None]
    citation_links = session.exec(
        select(TestPointCitation).where(TestPointCitation.test_point_id.in_(point_ids))
    ).all() if point_ids else []
    case_links = session.exec(
        select(TestPointCaseLink).where(TestPointCaseLink.test_point_id.in_(point_ids))
    ).all() if point_ids else []
    citations = session.exec(
        select(TaskCitation)
        .where(TaskCitation.task_id == task_id)
        .order_by(col(TaskCitation.id).asc())
    ).all()
    citation_by_point: dict[int, list[int]] = {}
    for link in citation_links:
        citation_by_point.setdefault(int(link.test_point_id), []).append(int(link.citation_id))
    cases_by_point: dict[int, list[int]] = {}
    for link in case_links:
        cases_by_point.setdefault(int(link.test_point_id), []).append(int(link.test_case_id))
    finalized = bool(task and (task.status == "finalized" or task.finalized_draft_id is not None))
    draft_cases_by_point = (
        {}
        if finalized
        else _draft_cases_by_point(session, task, checkpoint.status, list(points))
    )

    def point_is_covered(point_id: int) -> bool:
        if finalized:
            return bool(cases_by_point.get(point_id))
        return bool(draft_cases_by_point.get(point_id))

    selected = [row for row in points if row.is_selected and not row.is_excluded]
    point_outputs = []
    for row in points:
        case_ids = sorted(set(cases_by_point.get(int(row.id), [])))
        point_outputs.append(
            {
                "stable_key": row.stable_key,
                "title": row.title,
                "priority": row.priority,
                "dimension": row.dimension,
                "selected": bool(row.is_selected),
                "excluded": bool(row.is_excluded),
                "covered": point_is_covered(int(row.id)),
                "case_ids": case_ids,
                "citation_ids": sorted(set(citation_by_point.get(int(row.id), []))),
            }
        )
    covered = sum(1 for row in selected if point_is_covered(int(row.id)))
    selected_count = len(selected)
    citation_outputs = []
    for citation in citations:
        related_points = [
            row for row in points if int(citation.id) in citation_by_point.get(int(row.id), [])
        ]
        case_ids = sorted({case_id for row in related_points for case_id in cases_by_point.get(int(row.id), [])})
        citation_outputs.append(
            {
                "citation_id": int(citation.id),
                "title": citation.title,
                "path": citation.path,
                "test_point_keys": [row.stable_key for row in related_points],
                "case_ids": case_ids,
                "used": bool(related_points),
            }
        )
    return {
        "task_id": task_id,
        "total_test_points": len(points),
        "selected_test_points": selected_count,
        "covered_test_points": covered,
        "uncovered_test_points": selected_count - covered,
        "coverage_percent": round((covered / selected_count) * 100, 2) if selected_count else 0.0,
        "points": point_outputs,
        "citations": citation_outputs,
    }
