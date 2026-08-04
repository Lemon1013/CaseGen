"""Validated data contracts for Wiki 2.0 Markdown pages.

This module owns the small, filesystem-independent page contract used by later
Wiki ingestion tasks. A page key is an identifier, not a path supplied by an
LLM, and source references are structured so that rules can be traced back to
the immutable source chunks.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


WIKI_PAGE_TYPES = (
    "source",
    "rule",
    "entity",
    "scenario",
    "regression",
    "synthesis",
)
ALLOWED_PAGE_TYPES = frozenset(WIKI_PAGE_TYPES)
WikiPageType = Literal[
    "source",
    "rule",
    "entity",
    "scenario",
    "regression",
    "synthesis",
]

# A key is made of lowercase ASCII dot-separated segments. Hyphens and
# underscores are useful inside a segment, but may not begin or end one. In
# particular, this rejects slash, backslash, "..", drive-letter and absolute
# path forms.
_PAGE_KEY_RE = re.compile(
    r"[a-z0-9]+(?:[a-z0-9_-]*[a-z0-9])?"
    r"(?:\.[a-z0-9]+(?:[a-z0-9_-]*[a-z0-9])?)*"
)
_MAX_PAGE_KEY_LENGTH = 200


def validate_page_key(value: str) -> str:
    """Validate and return a stable Wiki page identifier.

    Page keys deliberately do not accept path syntax. Callers should use the
    key only as an identity and let a repository layer map it to a filename.
    """

    if not isinstance(value, str):
        raise ValueError("page_key must be a string")
    if value != value.strip():
        raise ValueError("page_key must not contain leading or trailing whitespace")
    if not value:
        raise ValueError("page_key must not be empty")
    if len(value) > _MAX_PAGE_KEY_LENGTH:
        raise ValueError(f"page_key must be at most {_MAX_PAGE_KEY_LENGTH} characters")
    if _PAGE_KEY_RE.fullmatch(value) is None:
        raise ValueError(
            "page_key must use lowercase dot-separated segments and contain "
            "no path characters"
        )
    return value


def is_valid_page_key(value: Any) -> bool:
    """Return whether value is a valid page key without raising."""

    try:
        validate_page_key(value)
    except (TypeError, ValueError):
        return False
    return True


def _as_list(value: Any, *, field_name: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, (str, bytes, bytearray)):
        return [value]
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def _normalise_strings(value: Any, *, field_name: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in _as_list(value, field_name=field_name):
        if not isinstance(item, str):
            raise TypeError(f"{field_name} must contain strings")
        item = item.strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


class WikiSource(BaseModel):
    """A traceable reference from a Wiki page to an uploaded document."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    document_id: int = Field(ge=1)
    chunk_ids: list[int] = Field(default_factory=list)
    clauses: list[str] = Field(default_factory=list)

    @field_validator("document_id", mode="before")
    @classmethod
    def _reject_boolean_document_id(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise TypeError("document_id must be a positive integer")
        return value

    @field_validator("chunk_ids", mode="before")
    @classmethod
    def _normalise_chunk_ids(cls, value: Any) -> list[Any]:
        values = _as_list(value, field_name="chunk_ids")
        if any(isinstance(item, bool) for item in values):
            raise TypeError("chunk_ids must contain positive integers")
        return values

    @field_validator("chunk_ids")
    @classmethod
    def _validate_chunk_ids(cls, value: list[int]) -> list[int]:
        if any(item < 1 for item in value):
            raise ValueError("chunk_ids must contain positive integers")
        return list(dict.fromkeys(value))

    @field_validator("clauses", mode="before")
    @classmethod
    def _normalise_clauses(cls, value: Any) -> list[str]:
        return _normalise_strings(value, field_name="clauses")


class WikiFrontmatter(BaseModel):
    """The validated YAML frontmatter attached to one Wiki page."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )

    page_key: str
    title: str = Field(min_length=1)
    type: WikiPageType
    domain: str | None = None
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    sources: list[WikiSource] = Field(default_factory=list)
    status: str = "draft"
    revision: int = Field(default=1, ge=1)
    updated_at: date | None = None

    @field_validator("page_key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        return validate_page_key(value)

    @field_validator("domain", "status")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value:
            raise ValueError("text fields in frontmatter must not be empty")
        return value

    @field_validator("aliases", "tags", mode="before")
    @classmethod
    def _normalise_string_fields(cls, value: Any, info: Any) -> list[str]:
        return _normalise_strings(value, field_name=info.field_name)

    @field_validator("sources", mode="before")
    @classmethod
    def _normalise_sources(cls, value: Any) -> list[Any]:
        return _as_list(value, field_name="sources")

    @model_validator(mode="after")
    def _require_rule_sources(self) -> "WikiFrontmatter":
        if self.type == "rule" and not self.sources:
            raise ValueError("rule pages must contain at least one valid source")
        return self


class WikiPage(BaseModel):
    """A Markdown page consisting of validated frontmatter and body text."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    frontmatter: WikiFrontmatter
    body: str = ""

    @field_validator("body")
    @classmethod
    def _normalise_body(cls, value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()

    @property
    def page_key(self) -> str:
        return self.frontmatter.page_key

    @property
    def title(self) -> str:
        return self.frontmatter.title

    @property
    def type(self) -> WikiPageType:
        return self.frontmatter.type


# Descriptive aliases for callers that prefer an explicit metadata name.
WikiPageMetadata = WikiFrontmatter
WikiFrontMatter = WikiFrontmatter


def validate_frontmatter(data: Mapping[str, Any] | WikiFrontmatter) -> WikiFrontmatter:
    """Validate frontmatter data and return its canonical Pydantic model."""

    if isinstance(data, WikiFrontmatter):
        return data.model_copy(deep=True)
    if not isinstance(data, Mapping):
        raise TypeError("frontmatter must be a mapping")
    return WikiFrontmatter.model_validate(dict(data))


def normalize_frontmatter(
    data: Mapping[str, Any] | WikiFrontmatter,
) -> dict[str, Any]:
    """Validate and return frontmatter in deterministic, de-duplicated form."""

    model = validate_frontmatter(data)
    return model.model_dump(mode="python", exclude_none=True)


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not isinstance(raw, str):
        raise TypeError("Wiki page content must be a string")

    text = raw.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("Wiki page must start with YAML frontmatter")

    closing = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing is None:
        raise ValueError("Wiki frontmatter is missing its closing delimiter")

    yaml_text = "\n".join(lines[1:closing]).strip()
    if not yaml_text:
        raise ValueError("Wiki frontmatter must not be empty")
    try:
        loaded = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ValueError("Invalid YAML frontmatter") from exc
    if not isinstance(loaded, Mapping):
        raise ValueError("Wiki frontmatter must be a YAML mapping")

    body = "\n".join(lines[closing + 1 :]).strip()
    return dict(loaded), body


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Parse, validate and normalize a Markdown page.

    The returned tuple is (frontmatter_dict, body). The dictionary uses Python
    values, for example date for an ISO date, and is safe to pass to
    serialize_frontmatter or serialize_wiki_page.
    """

    metadata, body = _split_frontmatter(raw)
    return normalize_frontmatter(metadata), body


def parse_wiki_page(raw: str) -> WikiPage:
    """Parse a complete Markdown Wiki page into a validated model."""

    metadata, body = parse_frontmatter(raw)
    return WikiPage(frontmatter=WikiFrontmatter.model_validate(metadata), body=body)


def serialize_frontmatter(
    data: Mapping[str, Any] | WikiFrontmatter,
) -> str:
    """Serialize validated frontmatter, including its YAML delimiters."""

    normalized = normalize_frontmatter(data)
    dumped = yaml.safe_dump(
        normalized,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=120,
    ).rstrip()
    return f"---\n{dumped}\n---"


def serialize_wiki_page(
    page: WikiPage | WikiFrontmatter | Mapping[str, Any],
    body: str | None = None,
) -> str:
    """Serialize a validated page and produce canonical Markdown text."""

    if isinstance(page, WikiPage):
        if body is not None:
            raise TypeError("body must be omitted when serializing a WikiPage")
        metadata = page.frontmatter
        page_body = page.body
    elif isinstance(page, WikiFrontmatter):
        metadata = page
        page_body = "" if body is None else body
    elif isinstance(page, Mapping):
        values = dict(page)
        embedded_body = values.pop("body", values.pop("content", None))
        if body is None:
            body = embedded_body
        metadata = validate_frontmatter(values)
        page_body = "" if body is None else body
    else:
        raise TypeError("page must be a WikiPage, WikiFrontmatter, or mapping")

    normalized_body = WikiPage(
        frontmatter=validate_frontmatter(metadata),
        body=page_body,
    ).body
    frontmatter = serialize_frontmatter(metadata)
    if not normalized_body:
        return f"{frontmatter}\n"
    return f"{frontmatter}\n\n{normalized_body}\n"


def validate_unique_page_keys(pages: Iterable[WikiPage | WikiFrontmatter]) -> None:
    """Raise if a batch contains duplicate stable page keys."""

    seen: set[str] = set()
    for page in pages:
        key = page.page_key
        if key in seen:
            raise ValueError(f"duplicate page_key: {key}")
        seen.add(key)


# Short aliases keep the contract convenient for repository code while the
# descriptive names above remain the documented API.
parse_page = parse_wiki_page
serialize_page = serialize_wiki_page
