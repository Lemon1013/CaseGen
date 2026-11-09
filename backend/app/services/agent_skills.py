"""Expert skill discovery for the agent chat (spec 3.4).

DSH resolves project skills by walking up from the session cwd to the nearest
`.git` directory and reading `.agents/skills/*/SKILL.md`.  CaseGen lists the
same files itself instead of proxying `GET /skills?root=project`, so the skill
picker works even when DSH is unreachable and never leaks a DSH error body.

Two measured DSH constraints drive the parsing here:

- `name` must be kebab-case; a value DSH's `isSkillName` rejects (for example
  a leading underscore) makes the skill invisible to the agent, so it is
  skipped here too rather than offered and silently ignored.
- `description` is required; without it DSH also skips the skill.

Frontmatter is parsed with `yaml.safe_load` (pyyaml is already a dependency).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

import yaml

from app import config

logger = logging.getLogger(__name__)

__all__ = [
    "SkillInfo",
    "list_skills",
    "find_skill",
    "skill_prompt_prefix",
    "skills_root",
]

_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Hard cap so a malformed SKILL.md cannot push an arbitrary body into a prompt.
_MAX_DESCRIPTION_CHARS = 1_000
_MAX_SKILLS = 200


class SkillInfo:
    """One parsed SKILL.md frontmatter block."""

    __slots__ = ("name", "description", "path")

    def __init__(self, name: str, description: str, path: str) -> None:
        self.name = name
        self.description = description
        self.path = path

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description, "path": self.path}


def skills_root() -> Path:
    """Return the repository-relative skills directory, re-read per call."""

    relative = (getattr(config, "AGENT_SKILLS_DIR", "") or ".agents/skills").strip()
    candidate = Path(relative).expanduser()
    return candidate if candidate.is_absolute() else Path(config.ROOT) / candidate


def _parse_frontmatter(text: str) -> Optional[dict[str, Any]]:
    """Extract the leading `---` YAML block, or None when absent/invalid."""

    stripped = text.lstrip("﻿")
    if not stripped.startswith("---"):
        return None
    lines = stripped.splitlines()
    end = -1
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            end = index
            break
    if end < 0:
        return None
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        logger.warning("SKILL.md frontmatter 解析失败，已跳过该技能。")
        return None
    return data if isinstance(data, dict) else None


def _read_skill_file(path: Path) -> Optional[SkillInfo]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("SKILL.md 读取失败，已跳过该技能。")
        return None
    data = _parse_frontmatter(raw)
    if data is None:
        return None
    name = str(data.get("name") or "").strip()
    description = str(data.get("description") or "").strip()
    if not name or not description:
        # DSH silently skips such a skill; offering it would be misleading.
        return None
    if not _SKILL_NAME_RE.match(name):
        return None
    try:
        relative = str(path.relative_to(Path(config.ROOT)))
    except ValueError:
        relative = path.name
    return SkillInfo(
        name=name,
        description=description[:_MAX_DESCRIPTION_CHARS],
        path=relative.replace("\\", "/"),
    )


def list_skills() -> list[SkillInfo]:
    """Every usable skill under the repository skills root, sorted by name."""

    root = skills_root()
    if not root.is_dir():
        return []
    skills: list[SkillInfo] = []
    try:
        candidates = sorted(root.glob("*/SKILL.md"))
    except OSError:
        logger.warning("技能目录不可读，返回空技能列表。")
        return []
    for candidate in candidates[:_MAX_SKILLS]:
        info = _read_skill_file(candidate)
        if info is not None:
            skills.append(info)
    skills.sort(key=lambda item: item.name)
    return skills


def find_skill(name: str) -> Optional[SkillInfo]:
    """Look one skill up by exact name."""

    wanted = (name or "").strip()
    if not wanted:
        return None
    for skill in list_skills():
        if skill.name == wanted:
            return skill
    return None


def skill_prompt_prefix(name: str) -> str:
    """Plain-text skill injection line (spec 3.4).

    Deliberately not a template: CaseGen has no interpolation mechanism and
    `default_prompts/optimize.md` forbids `{name}` / `{{name}}` / `${name}`
    placeholders, so the description is concatenated as literal text.
    """

    skill = find_skill(name)
    if skill is None:
        return ""
    return f"本次对话请以 {skill.description} 技能为准开展工作。\n\n"
