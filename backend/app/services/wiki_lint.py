"""Read-only Wiki consistency checks and candidate repair plans."""

from __future__ import annotations

import difflib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from sqlmodel import select

from app import config
from app.models.entities import WikiReviewItem, WikiPageRow
from app.services.wiki_index import build_index_entries, render_index
from app.services.wiki_schema import is_valid_page_key, parse_wiki_page, validate_page_key
from app.services.wiki_spaces import space_scope_clause

_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]")
_MDLINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


class LintReport(dict):
    @property
    def issues(self) -> list[dict[str, Any]]:
        return self["issues"]

    @property
    def candidate_diffs(self) -> list[dict[str, Any]]:
        return self["candidate_diffs"]

    @property
    def clean(self) -> bool:
        return not self["issues"]


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _reviews(session: Any, supplied: Iterable[Any] | None, space_id: int | None = None) -> list[Any]:
    if supplied is not None:
        return list(supplied)
    if session is None:
        return []
    try:
        statement = select(WikiReviewItem).order_by(WikiReviewItem.id)
        if space_id is not None:
            statement = statement.where(
                space_scope_clause(session, WikiReviewItem.space_id, space_id)
            )
        return list(session.exec(statement).all())
    except Exception:
        return []


def _issue(code: str, message: str, **fields: Any) -> dict[str, Any]:
    return {"code": code, "kind": code, "severity": "error", "message": message, **fields}


def _path_for(entry: Mapping[str, Any]) -> Path | None:
    path = entry.get("file_path")
    if path is None:
        return None
    return Path(path)


def _link_issues(entry: Mapping[str, Any], known: set[str], path_by_rel: Mapping[str, str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    content = str(entry.get("content") or "")
    for raw in _WIKILINK.findall(content):
        target = raw.strip()
        if not is_valid_page_key(target) or target not in known:
            result.append(_issue("dead_link", f"Wiki 链接目标不存在：{target}", page_key=entry.get("page_key"), target=target))
    for raw in _MDLINK.findall(content):
        target = raw.strip().split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:", "/wiki?", "#")):
            continue
        if target.endswith(".md") and target.replace("\\", "/") not in path_by_rel:
            result.append(_issue("dead_link", f"Markdown 链接目标不存在：{target}", page_key=entry.get("page_key"), target=target))
    return result


def _candidate(issue: Mapping[str, Any], *, index_before: str | None = None, index_after: str | None = None) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "operation": "review",
        "target": issue.get("page_key") or issue.get("target") or issue.get("code"),
        "reason": issue.get("message", ""),
        "apply": False,
    }
    if issue.get("code") == "index_drift":
        before = index_before or ""
        after = index_after or ""
        candidate.update({
            "operation": "rebuild_index",
            "target": "index.md",
            "before": before,
            "after": after,
            "diff": "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile="index.md", tofile="index.md (candidate)")),
        })
    return candidate


def lint_wiki(
    pages: Iterable[Any] | None = None,
    *,
    session: Any = None,
    review_items: Iterable[Any] | None = None,
    wiki_root: Path | str | None = None,
    index_path: Path | str | None = None,
    space_id: int | None = None,
) -> LintReport:
    """Return findings only.  This function never writes pages, index, or DB rows."""

    if session is None and hasattr(pages, "exec") and not isinstance(pages, (list, tuple, set)):
        session, pages = pages, None
    entries = build_index_entries(pages, session=session, space_id=space_id)
    issues: list[dict[str, Any]] = []
    by_key: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    path_by_rel: dict[str, str] = {}
    for entry in entries:
        by_key[str(entry.get("page_key") or "")].append(entry)
        path = str(entry.get("path") or "").replace("\\", "/")
        if path:
            path_by_rel[path] = str(entry.get("page_key") or "")
    for key, grouped in by_key.items():
        if not key or len(grouped) > 1:
            issues.append(_issue("duplicate_page_key", f"page_key 重复或缺失：{key or '<missing>'}", page_key=key or None, count=len(grouped)))

    known = {key for key in by_key if key}
    incoming: dict[str, int] = defaultdict(int)
    if wiki_root is None and space_id is not None:
        from app.services.wiki_spaces import resolve_space, space_root

        root = space_root(resolve_space(session, space_id))
    else:
        root = Path(wiki_root or config.WIKI_DIR)
    for entry in entries:
        issues.extend(_link_issues(entry, known, path_by_rel))
        source_count = int(entry.get("source_count") or 0)
        if entry.get("page_type") == "rule" and source_count == 0:
            issues.append(_issue("rule_without_source", f"规则页没有来源：{entry.get('page_key')}", page_key=entry.get("page_key")))
        for target in _WIKILINK.findall(str(entry.get("content") or "")):
            target = target.strip()
            if target in known:
                incoming[target] += 1
        path = _path_for(entry)
        if path is not None and not path.is_absolute():
            path = root / path
        if entry.get("explicit_path") and (path is None or not path.is_file()):
            issues.append(_issue("missing_file", f"Wiki 文件不存在：{entry.get('path') or entry.get('page_key')}", page_key=entry.get("page_key"), path=str(path or "")))
    for entry in entries:
        if entry.get("status") != "archived" and entry.get("page_type") != "source" and not incoming.get(str(entry.get("page_key")), 0) and int(entry.get("source_count") or 0) == 0:
            issues.append(_issue("orphan_page", f"页面没有来源或入链：{entry.get('page_key')}", page_key=entry.get("page_key")))

    for review in _reviews(session, review_items, space_id):
        status = str(_value(review, "status", "pending")).lower()
        kind = str(_value(review, "kind", "")).lower()
        reason = str(_value(review, "reason", ""))
        if status in {"pending", "open", "unresolved"} and (kind in {"conflict", "contradiction"} or "conflict" in kind or "冲突" in reason):
            issues.append(_issue("conflict", f"存在待审核冲突：{reason or kind}", page_id=_value(review, "page_id"), review_id=_value(review, "id"), reason=reason))

    expected = render_index(entries)
    target = Path(index_path or (root / "index.md"))
    actual = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else None
    if actual != expected:
        index_issue = _issue("index_drift", "index.md 与当前页面元数据不一致", path=str(target))
        issues.append(index_issue)

    candidates = [_candidate(item, index_before=actual, index_after=expected) for item in issues]
    return LintReport({"clean": not issues, "issues": issues, "candidate_diffs": candidates, "fixes": candidates, "checked_pages": len(entries)})


lint = lint_wiki
run_lint = lint_wiki


def candidate_diff_plan(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(report.get("candidate_diffs") or report.get("fixes") or [])


__all__ = ["LintReport", "candidate_diff_plan", "lint", "lint_wiki", "run_lint"]
