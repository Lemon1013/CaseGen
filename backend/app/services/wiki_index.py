"""Build the human-readable Wiki navigation index.

The index is a derived file.  SQLite/page frontmatter remains the source of
truth; this module only renders and writes ``data/wiki/index.md``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from sqlmodel import select

from app import config
from app.models.entities import WikiPageRow, WikiPageSource
from app.services.wiki_schema import parse_wiki_page
from app.services.wiki_repository import page_path

_SUMMARY_LIMIT = 140


def _get(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            found = value[name]
        else:
            found = getattr(value, name, None)
        if found is not None:
            return found
    return default


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return []


def _source_maps(session: Any) -> tuple[dict[int, list[int]], dict[int, int]]:
    ids: dict[int, list[int]] = {}
    counts: dict[int, int] = {}
    if session is None:
        return ids, counts
    try:
        sources = session.exec(select(WikiPageSource)).all()
    except Exception:
        return ids, counts
    for source in sources:
        if source.page_id is None:
            continue
        page_id = int(source.page_id)
        document_id = int(source.document_id)
        ids.setdefault(page_id, [])
        if document_id not in ids[page_id]:
            ids[page_id].append(document_id)
    counts = {page_id: len(values) for page_id, values in ids.items()}
    return ids, counts


def _page_metadata(page: Any) -> Any:
    metadata = _get(page, "frontmatter", default=None)
    return metadata if metadata is not None else page


def _page_content(page: Any, file_path: Path | None) -> str:
    content = _get(page, "content", "raw_content", default=None)
    if isinstance(content, str) and content:
        return content
    body = _get(page, "body", default=None)
    if isinstance(body, str) and body:
        return body
    if file_path is not None and file_path.is_file():
        try:
            file_path.resolve().relative_to(Path(config.WIKI_DIR).resolve())
        except ValueError:
            return ""
        try:
            return file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    return ""


def _relative_path(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.resolve().relative_to(Path(config.WIKI_DIR).resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _formal_path(page: Any, page_key: str, page_type: str) -> tuple[Path | None, bool]:
    raw = _get(page, "path", default=None)
    if raw:
        path = Path(str(raw))
        return (path if path.is_absolute() else Path(config.WIKI_DIR) / path, True)
    if page_key and page_type:
        try:
            return page_path(page_type, page_key), False
        except (TypeError, ValueError):
            pass
    return None, False


def _first_summary(content: str) -> str:
    text = content
    if text.lstrip().startswith("---"):
        try:
            text = parse_wiki_page(text).body
        except (TypeError, ValueError):
            pass
    blocks = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    for block in blocks:
        block = re.sub(r"^#{1,6}\s+", "", block).strip()
        block = re.sub(r"^[-*+]\s+", "", block).strip()
        if block:
            return re.sub(r"\s+", " ", block)[:_SUMMARY_LIMIT]
    return ""


def page_descriptor(
    page: Any,
    *,
    source_ids: Mapping[int, list[int]] | None = None,
    source_counts: Mapping[int, int] | None = None,
) -> dict[str, Any]:
    """Normalize a row, repository record, dict, or validated Wiki page."""

    metadata = _page_metadata(page)
    page_id = _get(page, "id", default=None)
    key = _get(metadata, "page_key", default=None) or _get(page, "page_key", default=None)
    if not key and page_id is not None:
        key = f"legacy.page.{page_id}"
    key = str(key or "")
    page_type = str(_get(page, "page_type", "type", default=None) or _get(metadata, "type", default="unknown"))
    path, explicit_path = _formal_path(page, key, page_type)
    content = _page_content(page, path)

    parsed_metadata = metadata
    if content.lstrip().startswith("---"):
        try:
            parsed_metadata = parse_wiki_page(content).frontmatter
        except (TypeError, ValueError):
            pass
    title = str(_get(page, "title", default=None) or _get(parsed_metadata, "title", default=None) or key or "Untitled")
    domain = _get(page, "domain", default=None) or _get(parsed_metadata, "domain", default=None) or "未分类"
    status = str(_get(page, "status", default=None) or _get(parsed_metadata, "status", default=None) or "published")

    raw_sources = _get(parsed_metadata, "sources", default=None)
    if raw_sources is None:
        raw_sources = _get(page, "sources", default=None)
    document_ids: list[int] = []
    for source in raw_sources or []:
        document_id = _get(source, "document_id", default=None)
        if document_id is not None:
            try:
                if int(document_id) not in document_ids:
                    document_ids.append(int(document_id))
            except (TypeError, ValueError):
                pass
    if page_id is not None and source_ids and int(page_id) in source_ids:
        for document_id in source_ids[int(page_id)]:
            if document_id not in document_ids:
                document_ids.append(document_id)
    fallback_document = _get(page, "source_document_id", default=None)
    if not document_ids and fallback_document is not None:
        try:
            document_ids = [int(fallback_document)]
        except (TypeError, ValueError):
            pass
    explicit_count = _get(page, "source_count", default=None)
    source_count = int(explicit_count) if explicit_count is not None else len(document_ids)
    if page_id is not None and source_counts and int(page_id) in source_counts:
        source_count = int(source_counts[int(page_id)])
    summary = str(_get(page, "summary", default=None) or _first_summary(content))
    return {
        "id": page_id,
        "page_key": key,
        "title": title,
        "page_type": page_type,
        "domain": str(domain),
        "status": status,
        "path": _relative_path(path),
        "explicit_path": explicit_path,
        "file_path": path,
        "summary": summary,
        "source_count": max(0, source_count),
        "source_document_ids": document_ids,
        "content": content,
    }


def _coerce_pages(pages: Any, session: Any) -> tuple[list[Any], Any]:
    if session is None and hasattr(pages, "exec") and not isinstance(pages, (list, tuple, set)):
        session = pages
        pages = None
    if pages is None:
        if session is None:
            return [], session
        pages = session.exec(select(WikiPageRow).order_by(WikiPageRow.id)).all()
    return list(pages), session


def build_index_entries(pages: Iterable[Any] | None = None, session: Any = None) -> list[dict[str, Any]]:
    pages, session = _coerce_pages(pages, session)
    source_ids, source_counts = _source_maps(session)
    entries = [page_descriptor(page, source_ids=source_ids, source_counts=source_counts) for page in pages]
    return sorted(entries, key=lambda item: (item["domain"], item["page_type"], item["title"], item["page_key"]))


def _md(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def _link(entry: Mapping[str, Any]) -> str:
    page_id = entry.get("id")
    if page_id is not None:
        return f"/wiki?page={page_id}"
    return str(entry.get("path") or "#").replace("\\", "/")


def render_index(entries: Iterable[Mapping[str, Any]]) -> str:
    """Render entries without touching the filesystem."""

    entries = list(entries)
    if not entries:
        return "# Wiki Index\n\n"
    lines = ["# Wiki Index", "", f"> 共 {len(entries)} 个页面。", ""]
    domains: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    for entry in entries:
        domains.setdefault(str(entry.get("domain") or "未分类"), {}).setdefault(
            str(entry.get("page_type") or "unknown"), []
        ).append(entry)
    for domain, types in sorted(domains.items()):
        lines.extend([f"## {domain}", ""])
        for page_type, pages in sorted(types.items()):
            lines.extend([f"### {page_type}", "", "| 页面 | 摘要 | 状态 | 来源数 |", "| --- | --- | --- | ---: |"])
            for entry in sorted(pages, key=lambda item: (str(item.get("title")), str(item.get("page_key")))):
                title = _md(entry.get("title") or entry.get("page_key") or "Untitled")
                lines.append(
                    f"| [{title}]({_link(entry)}) | {_md(entry.get('summary') or '—')} | "
                    f"{_md(entry.get('status') or 'published')} | {int(entry.get('source_count') or 0)} |"
                )
            lines.append("")
    return "\n".join(lines)


def rebuild_index(
    pages: Iterable[Any] | None = None,
    session: Any = None,
    index_path: Path | None = None,
) -> str:
    """Rewrite ``index.md`` while retaining compatibility with ``rows`` callers."""

    config.ensure_data_dirs()
    entries = build_index_entries(pages, session=session)
    content = render_index(entries)
    target = Path(index_path or (config.WIKI_DIR / "index.md"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return content


__all__ = ["build_index_entries", "page_descriptor", "rebuild_index", "render_index"]
