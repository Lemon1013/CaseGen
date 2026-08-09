from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

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
    # Compatibility clients may omit this; the API explicitly resolves the
    # default space while the new frontend always sends it.
    wiki_space_id: Optional[int] = Field(default=None, ge=1)


class ReviewResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    draft_id: int
    score: int
    verdict: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    requirement_id: int
    wiki_space_id: int
    wiki_space_name: str = ""
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
    latest_review: Optional[ReviewResultOut] = None
    created_at: datetime
    updated_at: datetime


class TaskCitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    path: str
    score: float = 0.0
    snippet: str = ""
    wiki_page_id: Optional[int] = None
    citation_type: str = "wiki"
    source_chunk_id: Optional[int] = None
    content_excerpt: str = ""
    clause_ids: List[str] = Field(default_factory=list)
    anchor_clause: Optional[str] = None
    available: bool = True
    legacy: bool = False
    legacy_reason: Optional[str] = None


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
    wiki_space_id: Optional[int] = None
    wiki_space_name: str = ""


class ApplyPromptBody(BaseModel):
    revision_id: int
    mode: Literal["global", "task_temp"]


class PromptRevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    base_prompt_id: Optional[int] = None
    new_content: str
    status: str
    created_at: datetime
