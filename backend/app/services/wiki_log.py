"""Append-only, parseable audit records for Wiki maintenance."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app import config

_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _path(
    path: Path | str | None,
    *,
    space_slug: str | None = None,
) -> Path:
    if path is not None:
        return Path(path)
    config.ensure_data_dirs()
    if space_slug:
        from app.services.wiki_spaces import space_root

        return space_root(space_slug) / "log.md"
    return Path(config.WIKI_DIR) / "log.md"


def append_event(
    event: str,
    details: Mapping[str, Any] | None = None,
    *,
    log_path: Path | str | None = None,
    at: str | None = None,
    space_id: int | None = None,
    space_slug: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Append one JSON object as a Markdown list item; never rewrite old events."""

    event = str(event).strip().lower()
    if not event:
        raise ValueError("event must not be empty")
    record: dict[str, Any] = {"at": at or _now(), "event": event}
    if details:
        record["details"] = dict(details)
    if space_id is not None:
        record["space_id"] = int(space_id)
    record.update(fields)
    target = _path(log_path, space_slug=space_slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = "- " + json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    with _LOCK:
        existing = target.read_text(encoding="utf-8") if target.exists() else "# Wiki Log\n\n"
        if existing and not existing.endswith("\n"):
            existing += "\n"
        if not existing:
            existing = "# Wiki Log\n\n"
        target.write_text(existing + line + "\n", encoding="utf-8")
    return record


append_log = append_event


def read_events(
    log_path: Path | str | None = None,
    *,
    space_slug: str | None = None,
) -> list[dict[str, Any]]:
    target = _path(log_path, space_slug=space_slug)
    if not target.exists():
        return []
    result: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if text.startswith("- "):
            text = text[2:].strip()
        if not text.startswith("{"):
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


read_log = read_events


def log_ingest(details: Mapping[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    return append_event("ingest", details, **fields)


def log_review(details: Mapping[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    return append_event("review", details, **fields)


def log_rollback(details: Mapping[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    return append_event("rollback", details, **fields)


def log_lint(details: Mapping[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    return append_event("lint", details, **fields)


__all__ = ["append_event", "append_log", "log_ingest", "log_lint", "log_review", "log_rollback", "read_events", "read_log"]
