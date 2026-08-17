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
