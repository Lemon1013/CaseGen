from __future__ import annotations

from pathlib import Path
from typing import Any

from app import config


def rebuild_index(pages: list[Any]) -> None:
    """Rewrite WIKI_DIR/index.md from page rows or dicts."""
    config.ensure_data_dirs()
    lines = ["# Wiki Index", ""]
    for p in pages:
        if isinstance(p, dict):
            title = p.get("title") or "Untitled"
            path = p.get("path") or ""
            page_type = p.get("page_type") or p.get("type") or ""
        else:
            title = getattr(p, "title", None) or "Untitled"
            path = getattr(p, "path", None) or ""
            page_type = getattr(p, "page_type", None) or ""
        # Prefer link relative to wiki root when path is under pages/
        link = path.replace("\\", "/")
        if link and not link.startswith("pages/") and not Path(link).is_absolute():
            # bare filename stored relative to pages/
            if not link.startswith("http"):
                link = f"pages/{Path(link).name}" if "/" not in link else link
        lines.append(f"- [{title}]({link}) `{page_type}`")
    if len(lines) == 2:
        lines.append("_empty_")
    lines.append("")
    index_path = config.WIKI_DIR / "index.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("\n".join(lines), encoding="utf-8")
