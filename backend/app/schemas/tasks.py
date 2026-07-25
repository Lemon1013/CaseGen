from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    """Create a generation task with an inline requirement."""

    title: str
    description: str
    focus_tags: List[str] = Field(default_factory=list)
    model_id: Optional[int] = None
    prompt_template_id: Optional[int] = None
    auto_review: bool = False
    run_generate: bool = False


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    requirement_id: int
    status: str
    model_id: Optional[int] = None
    prompt_template_id: Optional[int] = None
    error_message: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    focus_tags: List[str] = Field(default_factory=list)
    citation_count: int = 0
    latest_draft_snippet: Optional[str] = None
    latest_draft_version: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class CaseDraftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    version: int
    content_md: str
    prompt_version_ref: Optional[str] = None
    created_at: datetime


class TaskEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    step: str
    message: str
    detail_json: Optional[str] = None
    created_at: datetime
