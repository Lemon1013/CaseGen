"""Save one assistant message's structured cases directly to TestCase rows.

Unlike pipeline tasks, an agent conversation has no test-point checkpoint
chain and no longer creates a shell ``GenerationTask`` or ``CaseDraft``.
Instead, each assistant reply is parsed by ``message_id``, validated atomically,
and written straight into ``TestCase`` under either an existing project
``Requirement`` or a newly created ``Requirement``.

Atomicity still matches ``finalize_task``: any parse, validation, ownership,
or conflict failure raises before ``session.commit()`` so the caller rolls back
without leaving an empty requirement or partial case set behind.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Literal, Optional

from pydantic import ValidationError
from sqlmodel import Session

from app.models.entities import (
    AgentConversation,
    AgentMessage,
    Project,
    Requirement,
)
from app.schemas.agent import AgentDraftCaseIn, AgentDraftPointGroupIn
from app.services.case_management import (
    CaseDraftParseError,
    CaseKeyConflictError,
    import_cases_to_requirement,
    normalize_case_key,
    split_case_draft,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AgentDraftError",
    "extract_case_json_blocks",
    "parse_agent_case_groups",
    "render_draft_markdown",
    "save_message_cases_to_requirement",
]

# Failure codes the API layer maps onto HTTP statuses:
# `not_found` -> 404, `conflict` -> 409, everything else -> 422.
_JSON_FENCE_RE = re.compile(r"```json\s*([\s\S]*?)```", re.IGNORECASE)

_NO_ASSISTANT = "该条 Agent 回复内容为空，无法入库至用例管理。"
_INVALID_MESSAGE_ROLE = "仅支持将 Agent 回复入库至用例管理。"
_NO_JSON_BLOCK = (
    "本条回答中没有可入库的用例 JSON（需要 ```json 代码块）。"
    "你可以让 Agent 重新按用例 JSON 格式输出，或直接导出 Markdown 手动整理。"
)
_INVALID_JSON = (
    "用例 JSON 无法解析，入库已整体取消，未产生任何用例。"
    "请让 Agent 修正格式后重试，或导出 Markdown 手动整理。"
)
_INVALID_SHAPE = (
    "用例 JSON 结构不符合契约（需要测试点分组数组或用例数组），入库已整体取消。"
    "请让 Agent 按 DraftPointGroup/DraftCaseItem 结构重新输出，或导出 Markdown。"
)
_EMPTY_CASES = "用例 JSON 中没有可用用例（每组至少需要一条含步骤或用例编号的用例）。"
_DUPLICATE_KEYS = "用例编号重复：{keys}。入库已整体取消，请让 Agent 修正编号后重试。"
_NO_PROJECT = "会话所属项目不存在或已停用，无法入库用例。"
_INVALID_PROJECT = "会话的 project_id 不是有效的项目编号，无法入库用例。"

_MAX_CASE_KEY_CHARS = 60
_MAX_TITLE_CHARS = 400
_PRIORITY_VALUES = ("P0", "P1", "P2")


class AgentDraftError(ValueError):
    """Raised when an agent answer cannot be imported without data loss."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_draft",
        conflicts: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        self.code = str(code or "invalid_draft")
        self.conflicts = [dict(item) for item in conflicts] if conflicts else []
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

    pending = [
        line
        for item_text in case.pending_items
        for line in _block_lines(item_text)
    ]
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


def _project_id_of(conversation: AgentConversation) -> Optional[int]:
    raw = str(getattr(conversation, "project_id", "") or "").strip()
    if not raw.isdigit():
        return None
    return int(raw)


def save_message_cases_to_requirement(
    session: Session,
    conversation: AgentConversation,
    *,
    message_id: str,
    target_mode: Literal["existing_requirement", "new_requirement"],
    requirement_id: Optional[int] = None,
    requirement_title: Optional[str] = None,
    requirement_description: Optional[str] = None,
    conflict_strategy: Literal["error", "overwrite", "skip"] = "error",
    operator: Optional[str] = None,
    operator_user_id: Optional[int] = None,
) -> dict[str, Any]:
    """Import one assistant message's cases directly into ``TestCase``.

    Never creates a shell ``GenerationTask`` or ``CaseDraft``. Any parse,
    ownership, validation, or conflict error raises before ``session.commit()``,
    so the caller rolls back the transaction cleanly (including any flushed new
    ``Requirement``).
    """
    if conflict_strategy not in {"error", "overwrite", "skip"}:
        raise AgentDraftError("不支持的冲突处理策略。", code="invalid_strategy")

    project_id = _project_id_of(conversation)
    if project_id is None:
        raise AgentDraftError(_INVALID_PROJECT, code="invalid_project")
    project = session.get(Project, project_id)
    if project is None or getattr(project, "status", "active") != "active":
        raise AgentDraftError(_NO_PROJECT, code="invalid_project")

    message = session.get(AgentMessage, str(message_id))
    if message is None or str(message.conversation_id) != str(conversation.id):
        raise AgentDraftError("Resource not found in project", code="not_found")
    if message.role != "assistant":
        raise AgentDraftError(_INVALID_MESSAGE_ROLE, code="invalid_message_role")

    content = str(message.content or "")
    if not content.strip():
        raise AgentDraftError(_NO_ASSISTANT, code="no_assistant_message")

    groups = parse_agent_case_groups(content)
    content_md, _ = render_draft_markdown(groups)
    sections = split_case_draft(content_md)

    created_requirement = False
    if target_mode == "existing_requirement":
        if requirement_id is None or int(requirement_id) < 1:
            raise AgentDraftError("请选择要绑定的目标需求。", code="invalid_target")
        req = session.get(Requirement, int(requirement_id))
        if req is None or req.project_id != project_id:
            raise AgentDraftError("Resource not found in project", code="not_found")
    elif target_mode == "new_requirement":
        req_title = _one_line(requirement_title or "", 200)
        if not req_title:
            raise AgentDraftError("请输入新需求标题。", code="invalid_target")
        req_desc = str(requirement_description or "").strip()
        req = Requirement(
            project_id=project_id,
            title=req_title,
            description=req_desc,
            focus_tags_json="[]",
            source_filename=None,
        )
        session.add(req)
        session.flush()
        created_requirement = True
    else:
        raise AgentDraftError("不支持的归属目标模式。", code="invalid_target")

    try:
        imported = import_cases_to_requirement(
            session,
            int(req.id),
            sections,
            conflict_strategy=conflict_strategy,
            operator=operator,
            operator_user_id=operator_user_id,
            reason=f"agent:{conversation.id}:{message.id}",
        )
    except CaseKeyConflictError as exc:
        raise AgentDraftError(
            str(exc), code="conflict", conflicts=exc.conflicts
        ) from None

    session.commit()

    imported_count = int(imported["imported_count"])
    created_count = int(imported["created_count"])
    overwritten_count = int(imported["overwritten_count"])
    skipped_count = int(imported["skipped_count"])
    if overwritten_count == 0 and skipped_count == 0:
        summary = f"已入库 {imported_count} 条用例至需求「{req.title}」。"
    else:
        summary = (
            f"已处理 {imported_count + skipped_count} 条用例至需求「{req.title}」"
            f"（新增 {created_count}、覆盖 {overwritten_count}、跳过 {skipped_count}）。"
        )

    logger.info(
        "Agent 消息直入用例库：conversation=%s message=%s requirement=%s imported=%s skipped=%s",
        conversation.id,
        message.id,
        req.id,
        imported_count,
        skipped_count,
    )
    return {
        "requirement_id": int(req.id),
        "requirement_title": req.title,
        "created_requirement": created_requirement,
        "message_id": str(message.id),
        "conflict_strategy": conflict_strategy,
        "imported_count": imported_count,
        "created_count": created_count,
        "overwritten_count": overwritten_count,
        "skipped_count": skipped_count,
        "case_keys": list(imported["case_keys"]),
        "overwritten_keys": list(imported["overwritten_keys"]),
        "skipped_keys": list(imported["skipped_keys"]),
        "message": summary,
    }
