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


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, dict):
                # Prefer common label fields from LLM analysis blobs
                for key in ("name", "title", "rule", "text", "point", "hint"):
                    if item.get(key):
                        out.append(str(item[key]).strip())
                        break
                else:
                    out.append(str(item).strip())
            else:
                s = str(item).strip()
                if s:
                    out.append(s)
        return [x for x in out if x]
    return [str(value).strip()] if str(value).strip() else []


def pages_from_analysis(
    analysis: dict[str, Any],
    *,
    source_path: str,
    filename: str = "",
) -> list[dict[str, Any]]:
    """Deterministic wiki pages from Step-A analysis when wiki_write fails.

    Keeps compile usable when the second LLM call hits gateway 502/timeouts.
    """
    sources = [source_path] if source_path else []
    title_base = (
        str(analysis.get("summary_title") or "").strip()
        or (filename.rsplit(".", 1)[0] if filename else "")
        or "文档摘要"
    )
    key_rules = _as_str_list(analysis.get("key_rules"))
    api_points = _as_str_list(analysis.get("api_points"))
    test_hints = _as_str_list(analysis.get("test_hints"))
    entities = _as_str_list(analysis.get("entities"))
    suggested = _as_str_list(analysis.get("suggested_page_types"))
    tags = entities[:8] if entities else (["业务规则"] if key_rules else [])

    pages: list[dict[str, Any]] = []

    summary_lines = [f"# {title_base}", "", f"来源：`{source_path or filename or 'unknown'}`", ""]
    if key_rules:
        summary_lines.append("## 核心规则")
        summary_lines.extend(f"- {r}" for r in key_rules[:20])
        summary_lines.append("")
    if entities:
        summary_lines.append("## 关键实体")
        summary_lines.extend(f"- {e}" for e in entities[:20])
        summary_lines.append("")
    digest = str(analysis.get("global_digest") or "").strip()
    if digest:
        summary_lines.append("## 全局摘要")
        summary_lines.append(digest[:3000])
        summary_lines.append("")
    if suggested:
        summary_lines.append("## 建议页面类型")
        summary_lines.extend(f"- {s}" for s in suggested[:10])
        summary_lines.append("")
    pages.append(
        {
            "title": title_base,
            "type": "source_summary",
            "page_type": "source_summary",
            "sources": sources,
            "tags": tags,
            "body": "\n".join(summary_lines).strip(),
        }
    )

    if key_rules:
        rules_body = ["# 业务规则要点", ""] + [f"{i}. {r}" for i, r in enumerate(key_rules[:30], 1)]
        pages.append(
            {
                "title": f"{title_base}-业务规则",
                "type": "business",
                "page_type": "business",
                "sources": sources,
                "tags": tags,
                "body": "\n".join(rules_body).strip(),
            }
        )

    if api_points:
        api_body = ["# 接口/约束要点", ""] + [f"- {p}" for p in api_points[:30]]
        pages.append(
            {
                "title": f"{title_base}-接口要点",
                "type": "api",
                "page_type": "api",
                "sources": sources,
                "tags": tags + ["api"],
                "body": "\n".join(api_body).strip(),
            }
        )

    if test_hints:
        hint_body = ["# 测试设计提示", ""] + [f"- {h}" for h in test_hints[:30]]
        pages.append(
            {
                "title": f"{title_base}-测试提示",
                "type": "test_hint",
                "page_type": "test_hint",
                "sources": sources,
                "tags": tags + ["测试"],
                "body": "\n".join(hint_body).strip(),
            }
        )

    return pages[:8]
