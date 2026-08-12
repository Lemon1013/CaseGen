from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    """Create a task from an inline requirement or an existing requirement."""

    requirement_id: Optional[int] = Field(default=None, ge=1)
    title: Optional[str] = None
    description: Optional[str] = None
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
    finalized_draft_id: Optional[int] = None
    finalized_at: Optional[datetime] = None
    imported_case_ids: List[int] = Field(default_factory=list)
    imported_case_count: int = 0
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


class FinalizeTaskBody(BaseModel):
    """Optional exact draft selection for finalization.

    The body is optional at the route level for compatibility with the legacy
    empty POST; when omitted the latest draft is selected.
    """

    draft_id: Optional[int] = Field(default=None, ge=1)


class TestCaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    requirement_id: int
    case_key: str
    # ``source_case_key`` is retained as a first-class response field because
    # import identity must remain visible even after a manual edit.
    source_case_key: Optional[str] = None
    title: str = ""
    content_md: str
    # A small compatibility alias for clients that call the editable body
    # simply ``content``.
    content: Optional[str] = None
    status: str
    revision: int
    source_task_id: Optional[int] = None
    source_draft_id: Optional[int] = None
    archived_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class TestCaseUpdate(BaseModel):
    title: Optional[str] = None
    content_md: Optional[str] = None
    content: Optional[str] = None
    expected_revision: Optional[int] = Field(default=None, ge=1)
    revision: Optional[int] = Field(default=None, ge=1)
    expected_updated_at: Optional[datetime] = None
    reason: Optional[str] = None


class TestCaseOperationLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    test_case_id: int
    operation: str
    changed_fields: List[str] = Field(default_factory=list)
    before_hash: Optional[str] = None
    after_hash: Optional[str] = None
    before_length: Optional[int] = None
    after_length: Optional[int] = None
    added_lines: int = 0
    deleted_lines: int = 0
    title_changed: bool = False
    diff_summary: str = ""
    reason: Optional[str] = None
    operator: Optional[str] = None
    source_task_id: Optional[int] = None
    source_draft_id: Optional[int] = None
    source_case_key: Optional[str] = None
    created_at: datetime


class TestCaseCreate(BaseModel):
    requirement_id: int = Field(ge=1)
    case_key: str = Field(min_length=1)
    title: Optional[str] = None
    content_md: Optional[str] = None
    content: Optional[str] = None


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
    content: Optional[str] = Field(default=None, min_length=1)


class TaskModelUpdate(BaseModel):
    """Select the model used by the next generation attempt."""

    model_id: Optional[int] = Field(default=None, ge=1)


class PromptRevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    base_prompt_id: Optional[int] = None
    new_content: str
    status: str
    created_at: datetime
