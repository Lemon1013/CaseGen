"""Isolated staging areas for validated Wiki page candidates.

The staging layer deliberately knows nothing about SQLModel rows.  It accepts
one validated Markdown candidate, stores it below ``data/wiki/.staging`` and
parses it again before the caller is allowed to apply it to the formal Wiki.
Each operation receives its own directory so a failed candidate cannot be
mistaken for a page from another operation.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from app import config
from app.services.wiki_schema import (
    ALLOWED_PAGE_TYPES,
    WikiFrontmatter,
    WikiPage,
    parse_wiki_page,
    serialize_wiki_page,
    validate_page_key,
)


PAGE_TYPE_DIRECTORIES: dict[str, str] = {
    "source": "sources",
    "rule": "rules",
    "entity": "entities",
    "scenario": "scenarios",
    "regression": "regressions",
    "synthesis": "synthesis",
}


def _validate_page_type(page_type: str) -> str:
    if not isinstance(page_type, str) or page_type not in ALLOWED_PAGE_TYPES:
        allowed = ", ".join(sorted(ALLOWED_PAGE_TYPES))
        raise ValueError(f"unsupported Wiki page type: {page_type!r}; expected {allowed}")
    return page_type


def relative_page_path(page_type: str, page_key: str) -> Path:
    """Return the backend-owned relative path for a stable page identity."""

    page_type = _validate_page_type(page_type)
    page_key = validate_page_key(page_key)
    return Path(PAGE_TYPE_DIRECTORIES[page_type]) / f"{page_key}.md"


def _assert_inside(root: Path, candidate: Path) -> Path:
    root_resolved = root.resolve()
    candidate_resolved = candidate.resolve()
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("Wiki path must remain inside its configured root") from exc
    return candidate_resolved


@dataclass(frozen=True)
class StagedWikiPage:
    """A candidate file which has passed a second schema parse."""

    path: Path
    relative_path: str
    page: WikiPage
    content: str


class WikiStaging:
    """Context manager for one isolated Wiki candidate operation."""

    def __init__(self, operation_id: str | None = None) -> None:
        config.ensure_data_dirs()
        self.root = Path(config.WIKI_DIR) / ".staging"
        self.root.mkdir(parents=True, exist_ok=True)
        operation_name = operation_id or uuid4().hex
        if not operation_name or Path(operation_name).name != operation_name:
            raise ValueError("staging operation_id must be a single directory name")
        self.operation_dir = self.root / operation_name
        _assert_inside(self.root, self.operation_dir)
        self.operation_dir.mkdir(parents=True, exist_ok=False)
        self._cleaned = False

    @property
    def directory(self) -> Path:
        """Alias used by callers that prefer an explicit directory name."""

        return self.operation_dir

    def stage_page(
        self,
        page: WikiPage | WikiFrontmatter | Mapping[str, Any],
        body: str | None = None,
    ) -> StagedWikiPage:
        """Serialize a page and write it to this operation's staging area."""

        if isinstance(page, WikiPage):
            if body is not None:
                raise TypeError("body must be omitted when staging a WikiPage")
            parsed = page.model_copy(deep=True)
        elif isinstance(page, WikiFrontmatter):
            parsed = WikiPage(frontmatter=page.model_copy(deep=True), body=body or "")
        elif isinstance(page, Mapping):
            raw = serialize_wiki_page(page, body)
            parsed = parse_wiki_page(raw)
        else:
            raise TypeError("page must be a WikiPage, WikiFrontmatter, or mapping")

        raw = serialize_wiki_page(parsed)
        return self.stage_raw(
            raw,
            page_type=parsed.type,
            page_key=parsed.page_key,
        )

    def stage_raw(
        self,
        raw: str,
        *,
        page_type: str | None = None,
        page_key: str | None = None,
    ) -> StagedWikiPage:
        """Write raw candidate Markdown, then parse and validate it again.

        Expected identity arguments are supplied by the repository, which
        prevents an LLM-produced page from choosing a different destination
        between path calculation and validation.  If omitted, the page must
        first parse successfully so its own identity can be determined.
        """

        if not isinstance(raw, str):
            raise TypeError("staged Wiki content must be a string")

        if page_type is None or page_key is None:
            parsed_before_write = parse_wiki_page(raw)
            page_type = parsed_before_write.type
            page_key = parsed_before_write.page_key
        else:
            _validate_page_type(page_type)
            validate_page_key(page_key)

        relative = relative_page_path(page_type, page_key)
        candidate = self.operation_dir / relative
        _assert_inside(self.operation_dir, candidate)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(raw.encode("utf-8"))

        return self.validate_staged_page(
            candidate,
            page_type=page_type,
            page_key=page_key,
        )

    def validate_staged_page(
        self,
        path: Path,
        *,
        page_type: str | None = None,
        page_key: str | None = None,
    ) -> StagedWikiPage:
        """Read and schema-validate a candidate already in this area."""

        candidate = _assert_inside(self.operation_dir, Path(path))
        if not candidate.is_file():
            raise FileNotFoundError(f"staged Wiki page not found: {candidate}")
        content = candidate.read_text(encoding="utf-8")
        page = parse_wiki_page(content)
        if page_type is not None and page.type != _validate_page_type(page_type):
            raise ValueError("staged Wiki page type does not match its destination")
        if page_key is not None and page.page_key != validate_page_key(page_key):
            raise ValueError("staged Wiki page key does not match its destination")
        expected = relative_page_path(page.type, page.page_key).as_posix()
        actual = candidate.relative_to(self.operation_dir).as_posix()
        if actual != expected:
            raise ValueError("staged Wiki page path does not match its page identity")
        return StagedWikiPage(
            path=candidate,
            relative_path=actual,
            page=page,
            content=content,
        )

    def cleanup(self) -> None:
        """Remove only this operation and an empty staging root."""

        if self._cleaned:
            return
        root = self.root.resolve()
        operation = self.operation_dir.resolve()
        if operation.parent != root:
            raise ValueError("refusing to clean a staging directory outside the root")
        if operation.exists():
            shutil.rmtree(operation)
        try:
            if root.exists() and not any(root.iterdir()):
                root.rmdir()
        except OSError:
            # Another operation may have created a sibling between the check
            # and rmdir.  Its staging files are not ours to remove.
            pass
        self._cleaned = True

    def __enter__(self) -> "WikiStaging":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.cleanup()


# Descriptive aliases make the small staging API convenient for Task 7 while
# retaining one implementation and one cleanup contract.
WikiStagingArea = WikiStaging
new_staging_area = WikiStaging


__all__ = [
    "PAGE_TYPE_DIRECTORIES",
    "StagedWikiPage",
    "WikiStaging",
    "WikiStagingArea",
    "new_staging_area",
    "relative_page_path",
]
