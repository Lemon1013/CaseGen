from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class DataPoolCreate(BaseModel):
    wiki_space_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_kind: Literal["csv", "json", "description"]
    content: str = Field(min_length=1, max_length=2_000_000)
    parse_with_llm: bool = False


class DataPoolRevisionOut(BaseModel):
    id: int
    data_pool_id: int
    revision: int
    source_kind: str
    schema_data: dict[str, Any] = Field(serialization_alias="schema")
    records: list[Any]
    record_count: int
    content_hash: str
    status: str
    error_message: str | None
    model_ref: str | None
    prompt_ref: str | None
    created_at: datetime


class DataPoolOut(BaseModel):
    id: int
    wiki_space_id: int
    name: str
    description: str
    attributes: dict[str, Any]
    status: str
    latest_revision: DataPoolRevisionOut | None = None
    created_at: datetime
    updated_at: datetime


class PlatformCreate(BaseModel):
    wiki_space_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    artifact_topology: Literal["separated", "combined", "hybrid"]


class PlatformUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    artifact_topology: Literal["separated", "combined", "hybrid"] | None = None


class VariantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    applicability: str = Field(default="", max_length=4000)


class VariantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    applicability: str | None = Field(default=None, max_length=4000)


class ExampleCreate(BaseModel):
    kind: Literal["case", "data", "relation", "combined", "attachment"]
    name: str = Field(default="", max_length=200)
    media_type: str = Field(default="text/plain", max_length=120)
    content: str = Field(min_length=1, max_length=1_000_000)


class ExampleUpdate(BaseModel):
    kind: Literal["case", "data", "relation", "combined", "attachment"] | None = None
    name: str | None = Field(default=None, max_length=200)
    media_type: str | None = Field(default=None, min_length=1, max_length=120)
    content: str | None = Field(default=None, min_length=1, max_length=1_000_000)


class ExampleOut(BaseModel):
    id: int
    variant_id: int
    kind: str
    name: str
    media_type: str
    content: str
    content_hash: str
    created_at: datetime


class VariantOut(BaseModel):
    id: int
    platform_id: int
    name: str
    applicability: str
    status: str
    examples: list[ExampleOut] = Field(default_factory=list)
    created_at: datetime


class PlatformOut(BaseModel):
    id: int
    wiki_space_id: int
    name: str
    description: str
    artifact_topology: str
    status: str
    variants: list[VariantOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PlatformRenderRequest(BaseModel):
    wiki_space_id: int = Field(ge=1)
    platform_id: int = Field(ge=1)
    variant_id: int = Field(ge=1)
    test_case_ids: list[int] = Field(default_factory=list, max_length=100)
    external_case_markdown: str | None = Field(default=None, max_length=200_000)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    data_pool_revision_ids: list[int] = Field(default_factory=list, max_length=20)
    include_data_values: bool = False
    data_sample_limit: int = Field(default=10, ge=1, le=20)

    @field_validator("keywords")
    @classmethod
    def normalize_keywords(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            clean = item.strip()
            if not clean:
                continue
            if len(clean) > 80:
                raise ValueError("Each keyword must be at most 80 characters")
            if clean not in normalized:
                normalized.append(clean)
        return normalized

    @model_validator(mode="after")
    def require_case_input(self):
        markdown = (self.external_case_markdown or "").strip()
        self.external_case_markdown = markdown or None
        if not self.test_case_ids and not self.external_case_markdown:
            raise ValueError("Select semantic cases or provide external_case_markdown")
        return self


class SemanticCaseOut(BaseModel):
    id: int
    case_key: str
    title: str
    priority: str
    content_md: str


class ArtifactOut(BaseModel):
    id: int
    kind: str
    filename: str
    media_type: str
    content: str
    content_hash: str


class RenderRunOut(BaseModel):
    id: int
    wiki_space_id: int
    platform_id: int
    variant_id: int
    platform_name: str = ""
    variant_name: str = ""
    status: str
    generation_mode: str
    input_hash: str
    model_ref: str | None
    prompt_ref: str | None
    warnings: list[str] = Field(default_factory=list)
    error_message: str | None
    artifacts: list[ArtifactOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PlatformCaseCreate(BaseModel):
    wiki_space_id: int = Field(ge=1)
    platform_id: int = Field(ge=1)
    variant_id: int | None = Field(default=None, ge=1)
    filename: str = Field(min_length=1, max_length=240)
    name: str = Field(default="", max_length=200)
    kind: Literal["case", "data", "relation", "combined", "attachment"]
    media_type: str = Field(default="text/plain", min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=1_000_000)


class PlatformCaseUpdate(BaseModel):
    filename: str | None = Field(default=None, min_length=1, max_length=240)
    name: str | None = Field(default=None, max_length=200)
    kind: Literal["case", "data", "relation", "combined", "attachment"] | None = None
    media_type: str | None = Field(default=None, min_length=1, max_length=120)
    content: str | None = Field(default=None, min_length=1, max_length=1_000_000)


class PlatformCaseSummary(BaseModel):
    id: int
    wiki_space_id: int
    platform_id: int
    variant_id: int | None
    platform_name: str
    variant_name: str
    source_run_id: int | None
    source_artifact_id: int | None
    filename: str
    name: str
    kind: str
    media_type: str
    content_hash: str
    content_length: int
    status: str
    created_at: datetime
    updated_at: datetime


class PlatformCaseDetail(PlatformCaseSummary):
    content: str
    render_mode: Literal["json", "csv", "markdown", "text"]
    json_value: Any | None = None
    json_truncated: bool = False
    csv_headers: list[str] = Field(default_factory=list)
    csv_rows: list[list[str]] = Field(default_factory=list)
    csv_total_rows: int = 0
    csv_truncated: bool = False


class PlatformCasePage(BaseModel):
    items: list[PlatformCaseSummary]
    total: int
    limit: int
    offset: int
