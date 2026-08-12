from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.wiki_spaces import normalize_space_slug, validate_space_status


class WikiSpaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    slug: Optional[str] = Field(default=None, max_length=64)
    description: str = Field(default="", max_length=2000)

    @field_validator("name", "description")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str | None) -> str | None:
        return normalize_space_slug(value) if value else value


class WikiSpaceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("name", "description")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else value


class WikiSpaceStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        return validate_space_status(value)


class WikiSpaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    description: str = ""
    status: str
    document_count: int = 0
    page_count: int = 0
    pending_review_count: int = 0
    last_updated_at: datetime
    created_at: datetime
    updated_at: datetime


class WikiSpaceArchiveOut(WikiSpaceOut):
    pass
