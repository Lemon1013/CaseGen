"""Derived Wiki overview: domains, important rules, conflicts and gaps."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app import config
from app.models.entities import WikiReviewItem
from app.services.wiki_index import build_index_entries
from app.services.wiki_spaces import space_scope_clause


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _review_conflict(item: Any) -> bool:
    kind = str(_value(item, "kind", "")).lower()
    reason = str(_value(item, "reason", "")).lower()
    return _value(item, "status", "pending") in {"pending", "open", "unresolved"} and (
        kind in {"conflict", "contradiction"} or "conflict" in kind or "冲突" in reason or "contradict" in reason
    )


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


def collect_overview(
    pages: Iterable[Any] | None = None,
    *,
    session: Any = None,
    review_items: Iterable[Any] | None = None,
    space_id: int | None = None,
) -> dict[str, Any]:
    entries = build_index_entries(pages, session=session, space_id=space_id)
    by_id = {entry.get("id"): entry for entry in entries if entry.get("id") is not None}
    domains: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry.get("status") == "archived":
            continue
        domain = str(entry.get("domain") or "未分类")
        record = domains.setdefault(domain, {"domain": domain, "page_count": 0, "rule_count": 0, "source_count": 0})
        record["page_count"] += 1
        record["source_count"] += int(entry.get("source_count") or 0)
        if entry.get("page_type") == "rule":
            record["rule_count"] += 1

    rules = [entry for entry in entries if entry.get("page_type") == "rule" and entry.get("status") != "archived"]
    rules.sort(key=lambda item: (-int(item.get("source_count") or 0), str(item.get("title"))))
    main_rules = [
        {key: entry.get(key) for key in ("id", "page_key", "title", "domain", "summary", "status", "source_count")}
        for entry in rules[:50]
    ]
    conflicts: list[dict[str, Any]] = []
    for item in _reviews(session, review_items, space_id):
        if not _review_conflict(item):
            continue
        entry = by_id.get(_value(item, "page_id"))
        conflicts.append({
            "id": _value(item, "id"),
            "page_key": entry.get("page_key") if entry else None,
            "kind": _value(item, "kind", "conflict"),
            "status": _value(item, "status", "pending"),
            "reason": _value(item, "reason", ""),
        })

    gaps: list[dict[str, Any]] = []
    for entry in rules:
        if int(entry.get("source_count") or 0) == 0:
            gaps.append({"kind": "no_source_rule", "page_key": entry.get("page_key"), "title": entry.get("title"), "reason": "规则缺少来源证据"})
    for domain, record in sorted(domains.items()):
        has_source = any(item.get("domain") == domain and item.get("page_type") == "source" for item in entries)
        if has_source and record["rule_count"] == 0:
            gaps.append({"kind": "no_rule_for_domain", "domain": domain, "reason": "已有来源但尚未整理出规则页"})
    for conflict in conflicts:
        gaps.append({"kind": "unresolved_conflict", "page_key": conflict.get("page_key"), "reason": conflict.get("reason") or "存在待审核冲突"})
    return {
        "domains": sorted(domains.values(), key=lambda item: item["domain"]),
        "main_rules": main_rules,
        "conflicts": conflicts,
        "knowledge_gaps": gaps,
    }


def render_overview(data: Mapping[str, Any]) -> str:
    lines = ["# Wiki Overview", "", "## 领域", ""]
    domains = list(data.get("domains") or [])
    if domains:
        lines.extend(["| 领域 | 页面数 | 规则数 | 来源数 |", "| --- | ---: | ---: | ---: |"])
        for item in domains:
            lines.append(f"| {item.get('domain') or '未分类'} | {item.get('page_count', 0)} | {item.get('rule_count', 0)} | {item.get('source_count', 0)} |")
    else:
        lines.append("_暂无领域。_")
    lines.extend(["", "## 主要规则", ""])
    rules = list(data.get("main_rules") or [])
    if rules:
        for rule in rules:
            link = f"/wiki?page={rule['id']}" if rule.get("id") is not None else "#"
            lines.append(f"- [{rule.get('title') or rule.get('page_key')}]({link})：{rule.get('summary') or '暂无摘要'}（{rule.get('status') or 'published'}，来源 {rule.get('source_count', 0)}）")
    else:
        lines.append("_暂无规则。_")
    lines.extend(["", "## 冲突", ""])
    conflicts = list(data.get("conflicts") or [])
    if conflicts:
        for item in conflicts:
            lines.append(f"- {item.get('page_key') or '未关联页面'}：{item.get('reason') or '待审核冲突'}")
    else:
        lines.append("_暂无待处理冲突。_")
    lines.extend(["", "## 知识缺口", ""])
    gaps = list(data.get("knowledge_gaps") or [])
    if gaps:
        for item in gaps:
            subject = item.get("page_key") or item.get("domain") or "Wiki"
            lines.append(f"- {subject}：{item.get('reason') or item.get('kind') or '待补充'}")
    else:
        lines.append("_暂无已识别知识缺口。_")
    return "\n".join(lines) + "\n"


def rebuild_overview(
    pages: Iterable[Any] | None = None,
    *,
    session: Any = None,
    review_items: Iterable[Any] | None = None,
    overview_path: Path | None = None,
    space_id: int | None = None,
) -> str:
    config.ensure_data_dirs()
    content = render_overview(
        collect_overview(
            pages,
            session=session,
            review_items=review_items,
            space_id=space_id,
        )
    )
    if overview_path is None and space_id is not None:
        from app.services.wiki_spaces import resolve_space, space_root

        target = space_root(resolve_space(session, space_id)) / "overview.md"
    else:
        target = Path(overview_path or (config.WIKI_DIR / "overview.md"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return content


build_overview = collect_overview
write_overview = rebuild_overview

__all__ = ["build_overview", "collect_overview", "rebuild_overview", "render_overview", "write_overview"]
