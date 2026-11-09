"""Save an agent conversation answer as a structured case draft (spec 3.5 plan a).

The agent has no test-point checkpoint chain, so `finalize_task` cannot be
reused (it requires a confirmed `TaskTestPointCheckpoint`).  Instead this
module builds the same durable shape the pipeline produces:

    virtual Requirement  ->  shell GenerationTask(status='generated')
                           ->  CaseDraft(v N)  ->  TestCase rows

Atomicity inherits the `finalize_task` principle verbatim: *a malformed draft
must leave the task retryable and must never commit a partial case set*.  Every
row is created inside the caller's session and committed exactly once at the
end; any parse, validation or collision failure raises before that commit, so
the caller rolls the whole transaction back and the conversation stays usable.

The JSON contract mirrors `app.schemas.tasks` draft output models
(`DraftPointGroupOut` / `DraftCaseItemOut`) so the same structured draft UI can
render an agent answer.  Both a bare case array and a point-group array are
accepted, matching the frontend parser in
`frontend/src/components/agent/parseAgentCases.ts`.

Structured fields are authoritative for rendering: `raw_md` is accepted but not
emitted, so an agent cannot inject an arbitrary heading that `split_case_draft`
would treat as an extra case.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Optional

from pydantic import ValidationError
from sqlmodel import Session, col, func, select

from app.models.entities import (
    AgentConversation,
    AgentMessage,
    CaseDraft,
    GenerationTask,
    Project,
    Requirement,
    TestCase,
)
from app.schemas.agent import AgentDraftCaseIn, AgentDraftPointGroupIn
from app.services.case_management import (
    CaseDraftParseError,
    import_cases_from_draft,
    normalize_case_key,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AgentDraftError",
    "extract_case_json_blocks",
    "parse_agent_case_groups",
    "render_draft_markdown",
    "save_conversation_as_draft",
]

# Failure codes the API layer maps onto HTTP statuses.  `conflict` -> 409,
# everything else -> 422 with a directly presentable Chinese detail.
_JSON_FENCE_RE = re.compile(r"```json\s*([\s\S]*?)```", re.IGNORECASE)

_NO_ASSISTANT = "对话中还没有 Agent 回答，无法导入为草稿。"
_NO_JSON_BLOCK = (
    "本轮回答中没有可导入的用例 JSON（需要 ```json 代码块）。"
    "你可以让 Agent 重新按用例 JSON 格式输出，或直接导出 Markdown 手动整理。"
)
_INVALID_JSON = (
    "用例 JSON 无法解析，导入已整体取消，未产生任何用例。"
    "请让 Agent 修正格式后重试，或导出 Markdown 手动整理。"
)
_INVALID_SHAPE = (
    "用例 JSON 结构不符合契约（需要测试点分组数组或用例数组），导入已整体取消。"
    "请让 Agent 按 DraftPointGroup/DraftCaseItem 结构重新输出，或导出 Markdown。"
)
_EMPTY_CASES = "用例 JSON 中没有可用用例（每组至少需要一条含步骤或用例编号的用例）。"
_DUPLICATE_KEYS = "用例编号重复：{keys}。导入已整体取消，请让 Agent 修正编号后重试。"
_CONFLICT = (
    "用例编号已存在于本对话导入的用例集中：{keys}。"
    "导入已整体取消；请在用例列表中删除旧用例，或让 Agent 换用新的用例编号。"
)
_NO_PROJECT = "会话所属项目不存在，无法导入用例。"
_INVALID_PROJECT = "会话的 project_id 不是有效的项目编号，无法导入用例。"
_IMPORTED = "已从 Agent 回答导入 {count} 条用例（草稿 v{version}）。"

_MAX_CASE_KEY_CHARS = 60
_MAX_TITLE_CHARS = 400
_PRIORITY_VALUES = ("P0", "P1", "P2")


class AgentDraftError(ValueError):
    """Raised when an agent answer cannot become a draft without data loss."""

    def __init__(self, message: str, *, code: str = "invalid_draft") -> None:
        self.code = str(code or "invalid_draft")
        super().__init__(message)


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def extract_case_json_blocks(content: str) -> list[str]:
    """Return the raw bodies of every ```json fenced block, in order."""

    if not isinstance(content, str) or not content.strip():
        return []
    return [match.group(1).strip() for match in _JSON_FENCE_RE.finditer(content)]


def _decode_block(body: str) -> Optional[Any]:
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def _is_group_payload(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("cases"), list)


def _is_case_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return any(key in value for key in ("case_key", "steps", "title", "raw_md"))


def parse_agent_case_groups(content: str) -> list[AgentDraftPointGroupIn]:
    """Parse an assistant body into validated point groups.

    Accepts, in order of preference: a top-level array, `{"points_with_cases":
    [...]}` or `{"groups": [...]}`.  Elements that look like cases rather than
    groups are collected into a single synthetic group so a flat case array also
    imports.  Raises `AgentDraftError` for every non-importable shape.
    """

    blocks = extract_case_json_blocks(content)
    if not blocks:
        raise AgentDraftError(_NO_JSON_BLOCK, code="no_case_json")

    parsed_any = False
    groups: list[AgentDraftPointGroupIn] = []
    loose_cases: list[Any] = []
    for body in blocks:
        payload = _decode_block(body)
        if payload is None:
            continue
        candidates: Optional[list[Any]]
        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict) and isinstance(payload.get("points_with_cases"), list):
            candidates = payload["points_with_cases"]
        elif isinstance(payload, dict) and isinstance(payload.get("groups"), list):
            candidates = payload["groups"]
        else:
            candidates = None
        if candidates is None:
            continue
        parsed_any = True
        for item in candidates:
            if _is_group_payload(item):
                groups.append(item)
            elif _is_case_payload(item):
                loose_cases.append(item)

    if not parsed_any:
        raise AgentDraftError(_INVALID_JSON, code="invalid_case_json")
    if loose_cases:
        groups.append({"title": "", "cases": loose_cases})
    if not groups:
        raise AgentDraftError(_INVALID_SHAPE, code="invalid_case_json")

    validated: list[AgentDraftPointGroupIn] = []
    for index, raw in enumerate(groups):
        try:
            group = AgentDraftPointGroupIn.model_validate(raw)
        except ValidationError:
            raise AgentDraftError(_INVALID_JSON, code="invalid_case_json") from None
        if group.cases:
            group.stable_key = group.stable_key or f"agent-point-{index + 1}"
            validated.append(group)
    if not validated:
        raise AgentDraftError(_EMPTY_CASES, code="invalid_case_json")
    return validated


# ---------------------------------------------------------------------------
# Markdown rendering (must round-trip through case_management parsers)
# ---------------------------------------------------------------------------


def _one_line(value: str, limit: int) -> str:
    """Collapse a value to one line so it cannot break an anchored heading."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].strip()


def _block_lines(value: str) -> list[str]:
    """Render a free-text field as indented-safe bullet lines."""

    lines = [line.strip() for line in str(value or "").splitlines()]
    return [line for line in lines if line]


def _normalize_priority(value: str) -> str:
    candidate = str(value or "").strip().upper()
    return candidate if candidate in _PRIORITY_VALUES else "P1"


def _case_key(value: Optional[str], fallback_index: int) -> str:
    text = _one_line(value or "", _MAX_CASE_KEY_CHARS).upper()
    if not text:
        return f"TC-{fallback_index:03d}"
    try:
        return normalize_case_key(text)
    except CaseDraftParseError:
        return f"TC-{fallback_index:03d}"


def _render_case(case: AgentDraftCaseIn, key: str, title: str, group: AgentDraftPointGroupIn) -> str:
    """Render one case in the canonical format the parsers expect.

    Heading, flush-left metadata bullets, then `### 前置条件` / `### 测试数据` /
    `### 测试步骤` / `### 待定事项`.  `parse_case_detail` reads exactly these
    heading names (note: 待定事项, not the prompt's 待确认项), and its steps
    lookahead stops the step block at 预期结果/待定事项/备注 headings.
    """

    lines: list[str] = [f"## {key} {title}"]
    lines.append(f"- 优先级：{_normalize_priority(case.priority or group.priority)}")
    case_type = _one_line(case.type or group.dimension, 120)
    if case_type:
        lines.append(f"- 类型：{case_type}")
    goal = _one_line(case.verification_goal or group.verification_goal, 400)
    if goal:
        lines.append(f"- 验证目标：{goal}")
    if group.title:
        lines.append(f"- 关联测试点：{_one_line(group.stable_key or group.title, 120)}")
    lines.append("")

    preconditions = _block_lines(case.preconditions)
    lines.append("### 前置条件")
    lines.extend(f"- {item}" for item in preconditions) if preconditions else lines.append("无")
    lines.append("")

    test_data = _block_lines(case.test_data)
    lines.append("### 测试数据")
    lines.extend(f"- {item}" for item in test_data) if test_data else lines.append("无")
    lines.append("")

    lines.append("### 测试步骤")
    if case.steps:
        for index, step in enumerate(case.steps, start=1):
            action = _one_line(step.action, 4_000) or "（未提供操作说明）"
            lines.append(f"{index}. {action}")
            expected = _one_line(step.expected, 4_000)
            lines.append(f"   预期结果：{expected}" if expected else "   预期结果：无")
    else:
        lines.append("1. （未提供步骤）")
        lines.append("   预期结果：无")
    lines.append("")

    pending = [item for item in (_block_lines(p) for p in case.pending_items) if item]
    lines.append("### 待定事项")
    if pending:
        lines.extend(f"- {_one_line(item, 500)}" for item in pending)
    else:
        lines.append("无")
    return "\n".join(lines)


def render_draft_markdown(groups: Iterable[AgentDraftPointGroupIn]) -> tuple[str, list[str]]:
    """Render every validated group into draft Markdown plus its case keys.

    Keys are globally unique across the whole draft: `split_case_draft` rejects
    duplicates before any write, and `import_cases_from_draft` scopes uniqueness
    per requirement, so a repeat key from two groups would fail the import.
    """

    sections: list[str] = []
    keys: list[str] = []
    seen: dict[str, str] = {}
    index = 0
    for group in groups:
        for case in group.cases:
            index += 1
            key = _case_key(case.case_key, index)
            if key in seen:
                raise AgentDraftError(
                    _DUPLICATE_KEYS.format(keys=key), code="duplicate_case_key"
                )
            seen[key] = key
            title = _one_line(case.title or group.title or key, _MAX_TITLE_CHARS) or key
            sections.append(_render_case(case, key, title, group))
            keys.append(key)
    if not sections:
        raise AgentDraftError(_EMPTY_CASES, code="invalid_case_json")
    return "\n\n".join(sections) + "\n", keys


# ---------------------------------------------------------------------------
# Durable rows
# ---------------------------------------------------------------------------


def _next_draft_version(session: Session, task_id: int) -> int:
    """Replicated from task_pipeline._next_draft_version (module-private there)."""

    current = session.exec(
        select(func.max(CaseDraft.version)).where(CaseDraft.task_id == task_id)
    ).one()
    if current is None:
        return 1
    return int(current) + 1


def _project_id_of(conversation: AgentConversation) -> Optional[int]:
    raw = str(getattr(conversation, "project_id", "") or "").strip()
    if not raw.isdigit():
        return None
    return int(raw)


def _find_shell_task(session: Session, conversation_id: str) -> Optional[GenerationTask]:
    """Find the conversation's existing virtual requirement + shell task."""

    suffix = ":" + str(conversation_id)
    drafts = session.exec(
        select(CaseDraft).where(
            col(CaseDraft.prompt_version_ref).like("agent:%" + suffix)
        )
    ).all()
    for draft in drafts:
        task = session.get(GenerationTask, int(draft.task_id))
        if task is not None:
            return task
    return None


def _load_latest_assistant_content(session: Session, conversation_id: str) -> str:
    row = session.exec(
        select(AgentMessage)
        .where(AgentMessage.conversation_id == str(conversation_id))
        .where(AgentMessage.role == "assistant")
        .order_by(col(AgentMessage.created_at).desc(), col(AgentMessage.id).desc())
    ).first()
    return (row.content if row is not None else "") or ""


def save_conversation_as_draft(
    session: Session, conversation: AgentConversation
) -> dict[str, Any]:
    """Import the latest assistant answer as cases.  Never commits on failure.

    The caller owns the transaction: this function only flushes, and commits
    exactly once after the case import succeeded.  Any `AgentDraftError` or
    `CaseDraftParseError` leaves the session dirty, so the caller must roll back.
    """

    conversation_id = str(conversation.id)
    content = _load_latest_assistant_content(session, conversation_id)
    if not content.strip():
        raise AgentDraftError(_NO_ASSISTANT, code="no_assistant_message")

    groups = parse_agent_case_groups(content)
    content_md, case_keys = render_draft_markdown(groups)

    project_id = _project_id_of(conversation)
    if project_id is None:
        raise AgentDraftError(_INVALID_PROJECT, code="invalid_project")
    if session.get(Project, project_id) is None:
        raise AgentDraftError(_NO_PROJECT, code="no_project")

    task = _find_shell_task(session, conversation_id)
    if task is None:
        # A virtual Requirement is required because TestCase.requirement_id is
        # NOT NULL with a FK, while CaseDraft.task_id and
        # GenerationTask.requirement_id are NOT NULL without one.
        requirement = Requirement(
            project_id=project_id,
            title=_one_line(conversation.title or "未命名对话", 180) + "（Agent 对话导入）",
            description=(
                "由 Agent 对话导入生成的虚拟需求，用于承载对话产出的测试用例。\n"
                f"conversation_id={conversation_id}"
            ),
            source_filename=None,
        )
        session.add(requirement)
        session.flush()
        task = GenerationTask(
            requirement_id=int(requirement.id),
            project_id=project_id,
            # Set directly: the agent has no test-point checkpoint chain, so the
            # task_state machine and finalize_task are deliberately bypassed.
            status="generated",
        )
        session.add(task)
        session.flush()
    requirement_id = int(task.requirement_id)

    existing_keys = {
        str(row.case_key).upper()
        for row in session.exec(
            select(TestCase.case_key).where(TestCase.requirement_id == requirement_id)
        ).all()
    }
    collisions = sorted(key for key in case_keys if key.upper() in existing_keys)
    if collisions:
        raise AgentDraftError(_CONFLICT.format(keys="、".join(collisions)), code="conflict")

    skill = _one_line(conversation.skill_name or "", 80) or "none"
    draft = CaseDraft(
        task_id=int(task.id),
        version=_next_draft_version(session, int(task.id)),
        content_md=content_md,
        prompt_version_ref=f"agent:{skill}:{conversation_id}",
    )
    session.add(draft)
    session.flush()

    # Flush-only by contract: the import validates every collision before it
    # writes, and this function commits once so a malformed draft can never
    # leave a partially imported case set behind.
    imported = import_cases_from_draft(session, task, draft)
    session.commit()

    logger.info(
        "Agent 对话导入用例：conversation=%s task=%s draft=%s count=%s",
        conversation_id,
        task.id,
        draft.id,
        len(imported),
    )
    return {
        "task_id": int(task.id),
        "draft_id": int(draft.id),
        "requirement_id": requirement_id,
        "imported_count": len(imported),
        "case_keys": [str(row.case_key) for row in imported],
        "version": int(draft.version),
        "message": _IMPORTED.format(count=len(imported), version=draft.version),
    }
