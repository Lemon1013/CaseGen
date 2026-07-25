from __future__ import annotations

import re
from typing import Any

import yaml


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def split_wiki_pages(raw: str) -> list[dict[str, Any]]:
    """Parse LLM wiki-write output into page dicts.

    Expected multi-page format (YAML frontmatter blocks)::

        ---
        title: T1
        type: source_summary
        sources: ["raw/sources/a.md"]
        tags: ["余额"]
        ---
        body1
        ---
        title: T2
        type: business
        sources: ["raw/sources/a.md"]
        tags: []
        ---
        body2
    """
    text = _strip_code_fence(raw)
    if not text.strip():
        return []

    lines = text.splitlines()
    pages: list[dict[str, Any]] = []
    i = 0
    n = len(lines)

    def _is_fence(line: str) -> bool:
        return line.strip() == "---"

    while i < n:
        while i < n and not lines[i].strip():
            i += 1
        if i >= n:
            break
        if not _is_fence(lines[i]):
            # Skip orphan text until next frontmatter fence.
            i += 1
            continue
        i += 1  # past opening ---
        fm_lines: list[str] = []
        while i < n and not _is_fence(lines[i]):
            fm_lines.append(lines[i])
            i += 1
        if i >= n:
            break
        i += 1  # past closing ---
        body_lines: list[str] = []
        while i < n and not _is_fence(lines[i]):
            body_lines.append(lines[i])
            i += 1
        # Trim trailing blank lines from body.
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        body = "\n".join(body_lines).strip()

        meta: dict[str, Any] = {}
        fm_text = "\n".join(fm_lines).strip()
        if fm_text:
            try:
                loaded = yaml.safe_load(fm_text)
            except yaml.YAMLError:
                loaded = None
            if isinstance(loaded, dict):
                meta = loaded

        title = str(meta.get("title") or "").strip() or "Untitled"
        page_type = str(meta.get("type") or meta.get("page_type") or "business").strip()
        sources = meta.get("sources") or []
        tags = meta.get("tags") or []
        if not isinstance(sources, list):
            sources = [sources]
        if not isinstance(tags, list):
            tags = [tags]
        sources = [str(s) for s in sources]
        tags = [str(t) for t in tags]

        pages.append(
            {
                "title": title,
                "type": page_type,
                "page_type": page_type,
                "sources": sources,
                "tags": tags,
                "body": body,
            }
        )

    return pages


def parse_json_flexible(text: str) -> dict[str, Any]:
    """Parse JSON from raw LLM output (whole body or fenced block)."""
    import json

    cleaned = text.strip()
    if not cleaned:
        raise ValueError("Empty analysis JSON")

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.IGNORECASE)
    if fence:
        data = json.loads(fence.group(1).strip())
        if isinstance(data, dict):
            return data
        raise ValueError("Fenced JSON is not an object")

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        data = json.loads(cleaned[start : end + 1])
        if isinstance(data, dict):
            return data

    raise ValueError("Could not parse analysis JSON")
