from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class RequirementCreate(BaseModel):
    project_id: Optional[int] = Field(default=None, ge=1)
    title: str
    description: str
    focus_tags: List[str] = Field(default_factory=list)
    # Original requirement-document filename when the requirement content was
    # filled from an uploaded file; the file itself is archived by the
    # import-doc endpoint and never becomes a Document row.
    source_filename: Optional[str] = Field(default=None, max_length=255)


class RequirementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: Optional[int] = None
    title: str
    description: str
    focus_tags: List[str] = Field(default_factory=list)
    source_filename: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class RequirementDocImportOut(BaseModel):
    """Parsed requirement document returned by POST /api/requirements/import-doc."""

    title: str
    text: str
    char_count: int
    stored_path: str
    sha256: str
