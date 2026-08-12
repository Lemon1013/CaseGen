"""Human-facing Wiki titles independent from stable technical page keys."""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.services.wiki_schema import is_valid_page_key


PAGE_TYPE_LABELS = {
    "source": "来源",
    "rule": "规则",
    "entity": "实体",
    "scenario": "场景",
    "regression": "回归知识",
    "synthesis": "综合知识",
    "source_summary": "来源",
    "business": "规则",
    "api": "接口知识",
    "test_hint": "测试提示",
    "source_chunk": "原文",
}

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_MARKDOWN_PREFIX_RE = re.compile(r"^(?:#{1,6}|[-*+]|\d+[.)、])\s*")
_SOURCE_SUFFIX_RE = re.compile(r"[（(](?:条款|chunk|来源|页码)[^）)]*[）)]\s*$", re.I)


def page_type_label(page_type: str | None) -> str:
    return PAGE_TYPE_LABELS.get(str(page_type or "").strip(), str(page_type or "Wiki"))


def is_technical_title(title: str | None, page_key: str | None = None) -> bool:
    value = str(title or "").strip()
    if not value:
        return True
    if page_key and value.casefold() == str(page_key).strip().casefold():
        return True
    return is_valid_page_key(value)


def _clean_candidate(value: str, *, maximum: int = 42) -> str:
    text = _MARKDOWN_PREFIX_RE.sub("", str(value or "").strip())
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = _SOURCE_SUFFIX_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" ：:；;，,。.!！?？|-_")
    if len(text) > maximum:
        text = text[:maximum].rstrip(" ：:；;，,。.!！?？") + "…"
    return text


def _body_candidates(body: str) -> Iterable[str]:
    lines = str(body or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    in_frontmatter = bool(lines and lines[0].strip() == "---")
    for index, line in enumerate(lines):
        if in_frontmatter:
            if index > 0 and line.strip() == "---":
                in_frontmatter = False
            continue
        text = _clean_candidate(line)
        if text:
            yield text


def display_title(
    title: str | None,
    *,
    page_key: str | None = None,
    page_type: str | None = None,
    body: str = "",
    hints: Iterable[str] = (),
) -> str:
    """Choose a readable title, preferring Chinese semantic text over keys."""

    raw_title = _clean_candidate(str(title or ""))
    if raw_title and not is_technical_title(raw_title, page_key) and _CJK_RE.search(raw_title):
        return raw_title

    semantic_candidates = [
        *(_clean_candidate(str(item)) for item in hints if str(item).strip()),
        *_body_candidates(body),
    ]
    for candidate in semantic_candidates:
        if (
            candidate
            and not is_technical_title(candidate, page_key)
            and _CJK_RE.search(candidate)
        ):
            return candidate

    if raw_title and not is_technical_title(raw_title, page_key):
        return raw_title
    label = page_type_label(page_type)
    suffix = str(page_key or "").rsplit(".", 1)[-1].replace("-", " ").strip()
    return f"{label}：{suffix}" if suffix else label


__all__ = ["PAGE_TYPE_LABELS", "display_title", "is_technical_title", "page_type_label"]
