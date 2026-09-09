"""Current-state test-case import, editing and audit helpers.

The service deliberately has no version table.  A ``TestCase`` row is the
current state and operation logs provide the audit/diff trail needed by the
UI.  Keeping import logic here also lets the task pipeline and HTTP routes use
the same idempotency and parsing rules.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models.entities import (
    CaseDraft,
    DraftTestPointLink,
    GenerationTask,
    TestCase,
    TestCaseOperationLog,
    TestPoint,
    TestPointCaseLink,
    TaskTestPointCheckpoint,
)
from app.services.test_points import current_points, point_citation_ids


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CaseDraftParseError(ValueError):
    """Raised when a draft cannot be split without risking content loss."""


def normalize_case_key(value: str) -> str:
    """Normalize case identifiers for requirement-scoped uniqueness."""

    normalized = (value or "").strip().upper()
    if not normalized:
        raise CaseDraftParseError("case key cannot be empty")
    return normalized


# Deliberately anchored at the beginning of a line.  A heading in the body
# such as ``### TC-001`` is not silently treated as an imported case.  Accept
# both the documented ``## TC-001 - title`` form and the common model output
# ``## TC-001 title`` so finalization does not collapse a multi-case draft.
CASE_HEADING_RE = re.compile(
    r"^##[ \t]+(TC-[A-Za-z0-9][A-Za-z0-9_.-]*)(?:(?:[ \t]*(?:[-:：|])[ \t]*|[ \t]+)(.*?))?[ \t]*$",
    re.MULTILINE,
)


def _section_metadata(section: str, title: str) -> dict[str, Any]:
    priority_match = re.search(
        r"(?im)^\s*(?:[-*]\s*)?(?:优先级|priority)\s*[:：|]\s*(P[0-9]+)\b",
        section,
    )
    priority_present = priority_match is not None
    priority = priority_match.group(1).upper() if priority_match else "P1"
    if priority not in {"P0", "P1", "P2"}:
        priority = "P1"
    point_block = re.search(
        r"(?im)^\s*(?:[-*]\s*)?(?:关联测试点|测试点|test points?)\s*[:：|]\s*(.+)$",
        section,
    )
    source = point_block.group(1) if point_block else section
    point_keys = list(dict.fromkeys(re.findall(r"\bTP-[A-Za-z0-9][A-Za-z0-9_.-]*\b", source, re.IGNORECASE)))
    return {
        "priority": priority,
        "priority_present": priority_present and priority in {"P0", "P1", "P2"},
        "test_point_keys": [item.upper() for item in point_keys],
    }


def split_case_draft(content_md: str) -> list[dict[str, Any]]:
    """Split Markdown into case sections while preserving every character.

    A normal finalized draft contains one or more ``## TC-xxx`` headings.  For
    backwards compatibility with early generated drafts that had no such
    heading, the complete non-empty document is imported as ``TC-001`` rather
    than silently discarded.  Duplicate keys are rejected before any database
    write, making malformed input observable and retryable.
    """

    if not isinstance(content_md, str) or not content_md.strip():
        raise CaseDraftParseError("draft content is empty")

    matches = list(CASE_HEADING_RE.finditer(content_md))
    if not matches:
        # Preserve old one-case drafts in full.  This is explicit fallback,
        # not a lossy parser: the returned body is byte-for-byte equivalent
        # after only outer whitespace normalization.
        body = content_md.strip()
        return [{
            "case_key": "TC-001",
            "title": "TC-001",
            "content_md": body,
            **_section_metadata(body, "TC-001"),
        }]

    seen: set[str] = set()
    sections: list[dict[str, str]] = []
    preamble = content_md[: matches[0].start()].strip()
    for index, match in enumerate(matches):
        key = match.group(1).strip()
        folded = key.casefold()
        if folded in seen:
            raise CaseDraftParseError(f"duplicate case key in draft: {key}")
        seen.add(folded)

        end = matches[index + 1].start() if index + 1 < len(matches) else len(content_md)
        section = content_md[match.start() : end].strip()
        if index == 0 and preamble:
            # Keep title/frontmatter/instructions attached to the first case
            # so no content before the first heading disappears.
            section = f"{preamble}\n\n{section}"
        title = (match.group(2) or "").strip() or key
        sections.append({
            "case_key": key,
            "title": title,
            "content_md": section,
            **_section_metadata(section, title),
        })

    consumed = "\n\n".join(item["content_md"] for item in sections)
    if not consumed.strip():
        raise CaseDraftParseError("draft headings contain no content")
    return sections


def _line_change_counts(before: str, after: str) -> tuple[int, int]:
    """Return added/deleted line counts without retaining line contents."""

    before_lines = before.splitlines()
    after_lines = after.splitlines()
    added = deleted = 0
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            deleted += i2 - i1
        if tag in {"replace", "insert"}:
            added += j2 - j1
    return added, deleted


def _content_hash(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def append_case_log(
    session: Session,
    case: TestCase,
    operation: str,
    *,
    before: str | None = None,
    after: str | None = None,
    reason: str | None = None,
    operator: str | None = None,
    source_task_id: int | None = None,
    source_draft_id: int | None = None,
    source_case_key: str | None = None,
    changed_fields: Iterable[str] | None = None,
    title_changed: bool = False,
) -> TestCaseOperationLog:
    before_text = before or ""
    after_text = after or ""
    fields = {str(item) for item in (changed_fields or []) if str(item)}
    if before != after:
        fields.add("content_md")
    if title_changed:
        fields.add("title")
    if operation in {"archive", "restore"}:
        fields.add("status")
    added_lines, deleted_lines = _line_change_counts(before_text, after_text)
    if operation == "export":
        summary = "导出当前内容（未修改正文）"
    elif fields:
        summary = f"修改字段：{', '.join(sorted(fields))}"
        if added_lines or deleted_lines:
            summary += f"；新增 {added_lines} 行，删除 {deleted_lines} 行"
    else:
        summary = "记录操作，无正文变化"
    row = TestCaseOperationLog(
        test_case_id=int(case.id),
        operation=operation,
        changed_fields_json=json.dumps(sorted(fields), ensure_ascii=False),
        before_hash=_content_hash(before),
        after_hash=_content_hash(after),
        before_length=len(before) if before is not None else None,
        after_length=len(after) if after is not None else None,
        added_lines=added_lines,
        deleted_lines=deleted_lines,
        title_changed=title_changed,
        diff_summary=summary,
        reason=reason,
        operator=operator,
        source_task_id=source_task_id,
        source_draft_id=source_draft_id,
        source_case_key=source_case_key,
    )
    session.add(row)
    return row


def import_cases_from_draft(
    session: Session,
    task: GenerationTask,
    draft: CaseDraft,
) -> list[TestCase]:
    """Idempotently import a selected draft and return affected/current rows."""

    if draft.task_id != task.id:
        raise CaseDraftParseError("draft does not belong to task")
    if task.requirement_id is None:
        raise CaseDraftParseError("task has no requirement")

    sections = [
        {**section, "case_key": normalize_case_key(section["case_key"])}
        for section in split_case_draft(draft.content_md)
    ]
    current_checkpoint = session.exec(
        select(TaskTestPointCheckpoint)
        .where(
            TaskTestPointCheckpoint.task_id == task.id,
            TaskTestPointCheckpoint.status == "confirmed",
        )
        .order_by(col(TaskTestPointCheckpoint.attempt).desc())
    ).first()

    def ensure_point_links(case: TestCase, section: dict[str, Any]) -> None:
        keys = {str(item).upper() for item in section.get("test_point_keys") or []}
        if not keys or case.id is None or current_checkpoint is None:
            return
        points = session.exec(
            select(TestPoint)
            .where(TestPoint.checkpoint_id == current_checkpoint.id)
            .where(TestPoint.stable_key.in_(keys))
        ).all()
        for point in points:
            existing_draft = session.exec(
                select(DraftTestPointLink).where(
                    DraftTestPointLink.draft_id == draft.id,
                    DraftTestPointLink.case_key == section["case_key"],
                    DraftTestPointLink.test_point_id == point.id,
                )
            ).first()
            if existing_draft is None:
                session.add(DraftTestPointLink(
                    draft_id=int(draft.id),
                    case_key=section["case_key"],
                    test_point_id=int(point.id),
                ))
            existing_case = session.exec(
                select(TestPointCaseLink).where(
                    TestPointCaseLink.test_point_id == point.id,
                    TestPointCaseLink.test_case_id == case.id,
                )
            ).first()
            if existing_case is None:
                session.add(TestPointCaseLink(
                    test_point_id=int(point.id),
                    test_case_id=int(case.id),
                ))
    # Validate all requirement-scoped collisions before adding any rows so a
    # malformed multi-case draft cannot leave a partially imported set in a
    # caller's open transaction.
    for section in sections:
        key = section["case_key"]
        existing_source = session.exec(
            select(TestCase)
            .where(
                TestCase.source_task_id == task.id,
                TestCase.source_draft_id == draft.id,
                func.lower(func.trim(TestCase.source_case_key)) == key.casefold(),
            )
        ).first()
        if existing_source is not None:
            continue
        collision = session.exec(
            select(TestCase)
            .where(
                TestCase.requirement_id == int(task.requirement_id),
                func.lower(func.trim(TestCase.case_key)) == key.casefold(),
            )
        ).first()
        if collision is not None:
            raise CaseDraftParseError(
                f"case key {key} already exists for requirement {task.requirement_id}"
            )

    imported: list[TestCase] = []
    for section in sections:
        key = normalize_case_key(section["case_key"])
        existing = session.exec(
            select(TestCase)
            .where(
                TestCase.source_task_id == task.id,
                TestCase.source_draft_id == draft.id,
                func.lower(func.trim(TestCase.source_case_key)) == key.casefold(),
            )
            .order_by(col(TestCase.id).asc())
        ).first()
        if existing is not None:
            # Idempotent repeat: do not replace title/body/status/revision.
            ensure_point_links(existing, section)
            imported.append(existing)
            continue

        collision = session.exec(
            select(TestCase)
            .where(
                TestCase.requirement_id == int(task.requirement_id),
                func.lower(func.trim(TestCase.case_key)) == key.casefold(),
            )
            .order_by(col(TestCase.id).asc())
        ).first()
        if collision is not None:
            raise CaseDraftParseError(
                f"case key {key} already exists for requirement {task.requirement_id}"
            )

        case = TestCase(
            requirement_id=int(task.requirement_id),
            case_key=key,
            source_case_key=key,
            title=section["title"],
            content_md=section["content_md"],
            priority=section.get("priority") or "P1",
            source_task_id=task.id,
            source_draft_id=draft.id,
            status="active",
            revision=1,
        )
        session.add(case)
        session.flush()
        append_case_log(
            session,
            case,
            "import",
            before=None,
            after=case.content_md,
            source_task_id=task.id,
            source_draft_id=draft.id,
            source_case_key=key,
        )
        ensure_point_links(case, section)
        imported.append(case)
    session.flush()
    return imported


def cases_for_requirement(
    session: Session,
    requirement_id: int,
    *,
    include_archived: bool = False,
) -> list[TestCase]:
    statement = select(TestCase).where(TestCase.requirement_id == requirement_id)
    if not include_archived:
        statement = statement.where(TestCase.status != "archived")
    return list(
        session.exec(
            statement.order_by(
                col(TestCase.case_key).asc(), col(TestCase.id).asc()
            )
        ).all()
    )


def stable_cases(cases: Iterable[TestCase]) -> list[TestCase]:
    """Stable deterministic ordering for lists and exports."""

    return sorted(
        cases,
        key=lambda row: (
            int(row.requirement_id),
            (row.case_key or "").casefold(),
            int(row.id or 0),
        ),
    )


def parse_case_detail(section: dict[str, Any]) -> dict[str, Any]:
    """Extract structured details from a single case markdown section."""
    raw_md = section.get("content_md", "")
    case_key = section.get("case_key", "")
    title = section.get("title", case_key)
    priority = section.get("priority", "P1")

    # 1. 提取类型 (type)
    type_match = re.search(
        r"(?im)^\s*(?:[-*]\s*)?(?:类型|用例类型|type)\s*[:：|]\s*(.+)$",
        raw_md,
    )
    case_type = type_match.group(1).strip() if type_match else ""

    # 2. 提取验证目标 (verification_goal)
    goal_match = re.search(
        r"(?im)^\s*(?:[-*]\s*)?(?:验证目标|测试目标|目标|goal)\s*[:：|]\s*(.+)$",
        raw_md,
    )
    verification_goal = goal_match.group(1).strip() if goal_match else ""

    # 3. 提取前置条件 (preconditions)
    pre_match = re.search(
        r"(?im)^###?\s*前置条件[^\n]*\n([\s\S]*?)(?=^###?|\Z)",
        raw_md,
    )
    preconditions = pre_match.group(1).strip() if pre_match else ""
    if not preconditions:
        pre_inline = re.search(r"(?im)^\s*(?:[-*]\s*)?前置条件\s*[:：|]\s*(.+)$", raw_md)
        if pre_inline:
            preconditions = pre_inline.group(1).strip()

    # 4. 提取测试数据 (test_data)
    data_match = re.search(
        r"(?im)^###?\s*测试数据[^\n]*\n([\s\S]*?)(?=^###?|\Z)",
        raw_md,
    )
    test_data = data_match.group(1).strip() if data_match else ""

    # 5. 提取步骤与预期 (steps)
    steps_block_match = re.search(
        r"(?im)^###?\s*(?:测试步骤|步骤与预期|步骤)[^\n]*\n([\s\S]*?)(?=^###?\s*(?:预期结果|待定事项|待定项|备注)|\Z)",
        raw_md,
    )
    expected_block_match = re.search(
        r"(?im)^###?\s*(?:预期结果|预期断言|断言)[^\n]*\n([\s\S]*?)(?=^###?|\Z)",
        raw_md,
    )

    steps: list[dict[str, Any]] = []
    if steps_block_match:
        steps_text = steps_block_match.group(1).strip()
        raw_items = re.split(r"(?m)^(?=\s*\d+[.、)])", steps_text)
        step_idx = 1
        for item in raw_items:
            item_str = item.strip()
            if not item_str:
                continue
            cleaned = re.sub(r"^\s*\d+[.、)]\s*", "", item_str).strip()
            sub_exp_match = re.search(r"(?im)(?:[-*]\s*)?(?:预期结果|预期|断言)\s*[:：]\s*([\s\S]*)$", cleaned)
            if sub_exp_match:
                act = cleaned[: sub_exp_match.start()].strip()
                exp = sub_exp_match.group(1).strip()
            else:
                act = cleaned
                exp = ""
            steps.append({
                "step_no": step_idx,
                "action": act,
                "expected": exp,
            })
            step_idx += 1

    if expected_block_match and steps:
        exp_text = expected_block_match.group(1).strip()
        raw_exps = re.split(r"(?m)^(?=\s*\d+[.、)])", exp_text)
        exp_list = [re.sub(r"^\s*\d+[.、)]\s*", "", e.strip()).strip() for e in raw_exps if e.strip()]
        for i, st in enumerate(steps):
            if not st["expected"] and i < len(exp_list):
                st["expected"] = exp_list[i]

    # 6. 提取待定事项 (pending_items)
    pending_match = re.search(
        r"(?im)^###?\s*(?:待定事项|待定项|待定|pending)[^\n]*\n([\s\S]*?)(?=^###?|\Z)",
        raw_md,
    )
    pending_items: list[str] = []
    if pending_match:
        p_text = pending_match.group(1).strip()
        lines = [re.sub(r"^\s*[-*]\s*", "", ln).strip() for ln in p_text.splitlines() if ln.strip()]
        pending_items = [ln for ln in lines if ln]

    return {
        "case_key": case_key,
        "title": title,
        "priority": priority,
        "type": case_type,
        "verification_goal": verification_goal,
        "preconditions": preconditions,
        "test_data": test_data,
        "steps": steps,
        "pending_items": pending_items,
        "raw_md": raw_md,
    }


def aggregate_draft_cases_by_points(
    session: Session,
    task_id: int,
    draft: CaseDraft,
) -> list[dict[str, Any]]:
    """Group draft cases by test points, honoring links or falling back to regex."""
    try:
        sections = split_case_draft(draft.content_md)
    except Exception:
        sections = []

    case_details = [parse_case_detail(sec) for sec in sections]
    case_detail_by_key = {c["case_key"]: c for c in case_details}

    points = current_points(session, task_id)
    point_citation_map: dict[int, list[int]] = {}
    for pt in points:
        if pt.id is not None:
            point_citation_map[pt.id] = point_citation_ids(session, pt.id)

    links = session.exec(
        select(DraftTestPointLink).where(DraftTestPointLink.draft_id == draft.id)
    ).all()

    point_cases_map: dict[int, list[str]] = {pt.id: [] for pt in points if pt.id is not None}
    linked_case_keys: set[str] = set()

    for link in links:
        if link.test_point_id in point_cases_map:
            if link.case_key not in point_cases_map[link.test_point_id]:
                point_cases_map[link.test_point_id].append(link.case_key)
            linked_case_keys.add(link.case_key)

    point_by_stable_key = {pt.stable_key.upper(): pt for pt in points}
    for sec in sections:
        ck = sec["case_key"]
        if ck in linked_case_keys:
            continue
        keys = sec.get("test_point_keys") or []
        for k in keys:
            norm_k = k.upper()
            if norm_k in point_by_stable_key:
                pt_id = point_by_stable_key[norm_k].id
                if pt_id is not None:
                    if ck not in point_cases_map.setdefault(pt_id, []):
                        point_cases_map[pt_id].append(ck)
                    linked_case_keys.add(ck)

    groups: list[dict[str, Any]] = []
    for pt in points:
        c_keys = point_cases_map.get(pt.id or 0, [])
        cases = [case_detail_by_key[k] for k in c_keys if k in case_detail_by_key]
        groups.append({
            "test_point_id": pt.id,
            "stable_key": pt.stable_key,
            "title": pt.title,
            "verification_goal": pt.verification_goal,
            "dimension": pt.dimension,
            "priority": pt.priority,
            "citation_ids": point_citation_map.get(pt.id or 0, []),
            "cases": cases,
        })

    unlinked_keys = [c["case_key"] for c in case_details if c["case_key"] not in linked_case_keys]
    if unlinked_keys:
        unlinked_cases = [case_detail_by_key[k] for k in unlinked_keys if k in case_detail_by_key]
        groups.append({
            "test_point_id": None,
            "stable_key": "OTHER",
            "title": "其他衍生用例",
            "verification_goal": "未直接关联特定测试点的用例",
            "dimension": "other",
            "priority": "P1",
            "citation_ids": [],
            "cases": unlinked_cases,
        })

    return groups
