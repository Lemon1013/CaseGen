from __future__ import annotations

from pathlib import Path
from typing import Any

from app import config


def rebuild_index(pages: list[Any]) -> None:
    """Rewrite WIKI_DIR/index.md from page rows or dicts.

    Links prefer SPA-safe ``/wiki?page={id}`` so the CaseGen UI can open the
    page in-app. File paths are kept as a secondary hint in the code span.
    """
    config.ensure_data_dirs()
    lines = ["# Wiki Index", ""]
    for p in pages:
        if isinstance(p, dict):
            title = p.get("title") or "Untitled"
            path = p.get("path") or ""
            page_type = p.get("page_type") or p.get("type") or ""
            page_id = p.get("id")
        else:
            title = getattr(p, "title", None) or "Untitled"
            path = getattr(p, "path", None) or ""
            page_type = getattr(p, "page_type", None) or ""
            page_id = getattr(p, "id", None)

        file_link = path.replace("\\", "/")
        if file_link and not file_link.startswith("pages/") and not Path(file_link).is_absolute():
            if not file_link.startswith("http"):
                file_link = (
                    f"pages/{Path(file_link).name}" if "/" not in file_link else file_link
                )

        # Prefer in-app route so clicks never hit a bare /pages/*.md SPA 404
        if page_id is not None:
            link = f"/wiki?page={page_id}"
        elif file_link:
            link = file_link
        else:
            link = "#"

        type_bit = f" `{page_type}`" if page_type else ""
        lines.append(f"- [{title}]({link}){type_bit}")
    if len(lines) == 2:
        lines.append("_empty_")
    lines.append("")
    index_path = config.WIKI_DIR / "index.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("\n".join(lines), encoding="utf-8")
