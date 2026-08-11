from __future__ import annotations

import re
import json
from collections.abc import Mapping
from typing import Any

import yaml

from app.services.wiki_schema import is_valid_page_key, validate_page_key


class WikiPageParseError(ValueError):
    """Raised when Step B output is not a structured Wiki page collection."""


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


_TYPE_ALIASES = {
    # These aliases are accepted only at the compatibility boundary.  Formal
    # Wiki pages use the six types from wiki_schema.
    "source_summary": "source",
    "business": "rule",
    "api_rule": "rule",
    "test_hint": "regression",
}
_TARGET_PATH_FIELDS = frozenset(
    {
        "path",
        "file",
        "file_path",
        "filepath",
        "target_path",
        "target_file",
        "directory",
        "output_path",
    }
)
_PAGE_FIELDS = frozenset(
    {
        "operation_id",
        "page_key",
        "title",
        "type",
        "page_type",
        "domain",
        "aliases",
        "tags",
        "sources",
        "status",
        "body",
        "content",
        "markdown",
        "wikilinks",
        "frontmatter",
        "reason",
        "operation",
        "replace_existing",
    }
)


def _parse_json_value(raw: str) -> Any:
    """Parse a JSON value from plain, fenced, or prose-wrapped output."""

    cleaned = raw.strip()
    if not cleaned:
        raise WikiPageParseError("wiki_write output is empty")
    candidates = [cleaned]
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.IGNORECASE)
    if fence:
        candidates.insert(0, fence.group(1).strip())
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidates.append(cleaned[start : end + 1])
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start >= 0 and end > start:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise WikiPageParseError("wiki_write output is not valid JSON")


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else value
    if isinstance(values, Mapping):
        values = [values]
    try:
        values = list(values)
    except TypeError:
        values = [values]
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item).strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _page_list_from_value(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        for key in ("pages", "candidate_pages", "page_candidates", "items"):
            if key in value:
                nested = value[key]
                if not isinstance(nested, list):
                    raise WikiPageParseError(f"{key} must be a list")
                return nested
        if "page_key" in value or "frontmatter" in value:
            return [value]
        raise WikiPageParseError("wiki_write JSON must contain pages")
    if isinstance(value, list):
        return value
    raise WikiPageParseError("wiki_write JSON must be an object or list")


def _normalise_structured_page(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WikiPageParseError("each Wiki page candidate must be an object")
    page = dict(value)
    forbidden = _TARGET_PATH_FIELDS.intersection(page)
    if forbidden:
        names = ", ".join(sorted(forbidden))
        raise WikiPageParseError(f"LLM cannot choose formal Wiki path: {names}")
    unknown = set(page) - _PAGE_FIELDS
    if unknown:
        raise WikiPageParseError(
            "unknown Wiki page fields: " + ", ".join(sorted(unknown))
        )

    nested = page.pop("frontmatter", None)
    if nested is not None:
        if not isinstance(nested, Mapping):
            raise WikiPageParseError("frontmatter must be an object")
        nested = dict(nested)
        nested_forbidden = _TARGET_PATH_FIELDS.intersection(nested)
        if nested_forbidden:
            names = ", ".join(sorted(nested_forbidden))
            raise WikiPageParseError(f"LLM cannot choose formal Wiki path: {names}")
        nested_unknown = set(nested) - {
            "page_key",
            "title",
            "type",
            "page_type",
            "domain",
            "aliases",
            "tags",
            "sources",
            "status",
        }
        if nested_unknown:
            raise WikiPageParseError(
                "unknown Wiki frontmatter fields: "
                + ", ".join(sorted(nested_unknown))
            )
        merged = dict(nested)
        merged.update(page)
        page = merged

    body_values = [page.pop(name, None) for name in ("body", "content", "markdown")]
    body = next((item for item in body_values if item is not None), "")
    if not isinstance(body, str):
        raise WikiPageParseError("Wiki page body must be a string")
    if not body.strip():
        raise WikiPageParseError("Wiki page body must not be empty")
    page_key = str(page.get("page_key") or "").strip()
    if not page_key or not is_valid_page_key(page_key):
        raise WikiPageParseError(f"invalid page_key: {page_key or '<empty>'}")
    page_type = str(page.get("type") or page.get("page_type") or "").strip()
    page_type = _TYPE_ALIASES.get(page_type, page_type)
    if page_type not in {"source", "rule", "entity", "scenario", "regression", "synthesis"}:
        raise WikiPageParseError(f"unsupported Wiki page type: {page_type or '<empty>'}")
    title = str(page.get("title") or "").strip()
    if not title:
        raise WikiPageParseError(f"page title is required: {page_key}")
    sources = page.get("sources")
    if sources is None:
        sources = []
    elif isinstance(sources, (str, Mapping)):
        sources = [sources]
    elif not isinstance(sources, list):
        raise WikiPageParseError(f"sources must be a list: {page_key}")
    tags = _as_string_list(page.get("tags"))
    aliases = _as_string_list(page.get("aliases"))
    wikilinks = _as_string_list(page.get("wikilinks"))
    result: dict[str, Any] = {
        "page_key": validate_page_key(page_key),
        "title": title,
        "type": page_type,
        "page_type": page_type,
        "domain": page.get("domain"),
        "aliases": aliases,
        "tags": tags,
        "sources": sources,
        "status": str(page.get("status") or "published").strip() or "published",
        "wikilinks": wikilinks,
        "body": body.replace("\r\n", "\n").replace("\r", "\n").strip(),
    }
    if page.get("reason") is not None:
        result["reason"] = str(page["reason"]).strip()
    if page.get("operation") is not None:
        operation = str(page["operation"]).strip()
        if operation not in {"create", "update", "noop"}:
            raise WikiPageParseError(f"unsupported Wiki operation: {operation}")
        result["operation"] = operation
    result["replace_existing"] = bool(page.get("replace_existing", False))
    return result


def parse_wiki_write_output(
    raw: str | Mapping[str, Any] | list[Any],
    *,
    max_pages: int = 8,
    allow_legacy_markdown: bool = True,
) -> list[dict[str, Any]]:
    """Parse the Step B JSON page contract.

    The formal writer contract is JSON with structured page fields.  A
    frontmatter Markdown response is still accepted when called through the
    old ``split_wiki_pages`` path, so existing ingest callers remain usable.
    This function itself adds stable keys and canonical page types and never
    accepts a model-supplied destination path.
    """

    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    if isinstance(raw, str):
        try:
            value = _parse_json_value(raw)
        except WikiPageParseError:
            if not allow_legacy_markdown:
                raise
            legacy = split_wiki_pages(raw)
            if not legacy:
                raise
            # Legacy pages do not necessarily have a stable key.  Keep them
            # parseable for the old ingest caller, but require page_key for
            # the new application boundary below.
            return legacy[:max_pages]
    else:
        value = raw
    raw_pages = _page_list_from_value(value)
    if not raw_pages:
        raise WikiPageParseError("wiki_write returned no pages")
    if len(raw_pages) > max_pages:
        raise WikiPageParseError(
            f"too many Wiki pages: {len(raw_pages)} (maximum {max_pages})"
        )
    return [_normalise_structured_page(item) for item in raw_pages]


def reconcile_wiki_write_output(
    raw: str | Mapping[str, Any] | list[Any],
    operations: list[Mapping[str, Any]],
    *,
    max_pages: int = 8,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Join untrusted Step B prose to server-owned page operations.

    Page identity, operation, type and sources always come from the validated
    Step A plan.  A model may omit or alter those fields without aborting the
    ingest; unusable bodies simply fall back to deterministic rendering.
    """

    if isinstance(raw, str):
        value = _parse_json_value(raw)
    else:
        value = raw
    raw_pages = _page_list_from_value(value)
    warnings: list[str] = []
    tasks: list[dict[str, Any]] = []
    for position, operation in enumerate(operations, start=1):
        if not isinstance(operation, Mapping):
            continue
        op = str(operation.get("op") or operation.get("operation") or "")
        page_key = str(operation.get("page_key") or "").strip()
        if op not in {"create", "update"} or not page_key:
            continue
        tasks.append(
            {
                **dict(operation),
                "operation_id": str(operation.get("operation_id") or f"op-{position}"),
            }
        )
    tasks = tasks[:max_pages]
    by_id = {str(task["operation_id"]): task for task in tasks}
    by_key = {str(task["page_key"]): task for task in tasks}
    used: set[str] = set()
    candidates: list[dict[str, Any]] = []

    for position, raw_page in enumerate(raw_pages, start=1):
        if not isinstance(raw_page, Mapping):
            warnings.append(f"Step B pages[{position}] 不是对象，已忽略")
            continue
        page = dict(raw_page)
        nested = page.pop("frontmatter", None)
        if isinstance(nested, Mapping):
            merged = dict(nested)
            merged.update(page)
            page = merged
        forbidden = sorted(_TARGET_PATH_FIELDS.intersection(page))
        if forbidden:
            warnings.append(
                f"Step B pages[{position}] 路径字段已忽略：{', '.join(forbidden)}"
            )
        returned_operation_id = str(page.get("operation_id") or "").strip()
        returned_page_key = str(page.get("page_key") or "").strip()
        task = by_id.get(returned_operation_id)
        if task is None:
            task = by_key.get(returned_page_key)
        if task is None and (returned_operation_id or returned_page_key):
            warnings.append(
                f"Step B pages[{position}] 指向未知服务端任务，已忽略"
            )
            continue
        if task is None and position <= len(tasks):
            task = tasks[position - 1]
            warnings.append(
                f"Step B pages[{position}] 未提供有效 operation_id，已按计划顺序绑定"
            )
        if task is None:
            warnings.append(f"Step B pages[{position}] 没有对应服务端任务，已忽略")
            continue
        task_id = str(task["operation_id"])
        if task_id in used:
            warnings.append(f"Step B 重复返回 {task_id}，后续候选已忽略")
            continue

        body = page.get("body", page.get("content", page.get("markdown", "")))
        if not isinstance(body, str) or not body.strip():
            warnings.append(f"Step B {task_id} 正文为空，将使用确定性正文")
            continue
        expected_key = str(task["page_key"])
        returned_key = returned_page_key
        if returned_key and returned_key != expected_key:
            warnings.append(
                f"Step B {task_id} page_key {returned_key} 已覆盖为 {expected_key}"
            )
        expected_op = str(task.get("op") or task.get("operation"))
        returned_op = str(page.get("operation") or "").strip()
        if returned_op and returned_op != expected_op:
            warnings.append(
                f"Step B {task_id} operation {returned_op} 已覆盖为 {expected_op}"
            )

        used.add(task_id)
        candidates.append(
            {
                "operation_id": task_id,
                "operation": expected_op,
                "page_key": expected_key,
                "title": str(page.get("title") or "").strip(),
                "type": str(task.get("page_type") or "").strip(),
                "domain": page.get("domain"),
                "aliases": _as_string_list(page.get("aliases")),
                "tags": _as_string_list(page.get("tags")),
                # Source evidence is attached from the real Step A window by
                # the apply service; model-supplied source ids are ignored.
                "sources": [],
                "status": "published",
                "replace_existing": False,
                "body": body.replace("\r\n", "\n").replace("\r", "\n").strip(),
                "reason": str(page.get("reason") or task.get("reason") or "").strip(),
            }
        )

    missing = [str(task["operation_id"]) for task in tasks if str(task["operation_id"]) not in used]
    if missing:
        warnings.append(
            "Step B 未返回以下页面正文，将使用确定性候选：" + ", ".join(missing)
        )
    return candidates, warnings


# Names used by the apply service and by callers migrating from early Task 7
# prototypes.  They intentionally point to the same parser and contract.
parse_candidate_pages = parse_wiki_write_output
parse_page_candidates = parse_wiki_write_output
parse_structured_pages = parse_wiki_write_output


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
