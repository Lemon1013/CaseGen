from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskCreate(BaseModel):
    """Create a task from an inline requirement or an existing requirement."""

    requirement_id: Optional[int] = Field(default=None, ge=1)
    title: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=20000)
    # None means “not provided”; [] is an explicit request to clear tags on
    # an existing requirement.
    focus_tags: Optional[List[str]] = Field(default=None, max_length=30)
    model_id: Optional[int] = Field(default=None, ge=1)
    prompt_template_id: Optional[int] = Field(default=None, ge=1)
    auto_review: bool = False
    run_generate: bool = False
    # Compatibility clients may omit this; the API explicitly resolves the
    # default space while the new frontend always sends it.
    wiki_space_id: Optional[int] = Field(default=None, ge=1)
    generation_granularity: Literal["compact", "standard", "detailed"] = "standard"
    test_dimensions: List[str] = Field(default_factory=lambda: ["positive", "negative", "boundary"])
    # ``dimensions`` is accepted as a short-lived compatibility spelling for
    # clients built against the design draft.  The response always exposes
    # the canonical ``test_dimensions`` field.
    dimensions: Optional[List[str]] = None
    reference_case_ids: List[int] = Field(default_factory=list, max_length=10)
    reference_text: str = Field(default="", max_length=16000)

    @field_validator("focus_tags")
    @classmethod
    def validate_focus_tags(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return None
        if any(not isinstance(item, str) or len(item.strip()) > 80 for item in value):
            raise ValueError("Each focus tag must be at most 80 characters")
        return [item.strip() for item in value]

    @field_validator("reference_case_ids")
    @classmethod
    def validate_reference_case_ids(cls, value: List[int]) -> List[int]:
        if any(item <= 0 for item in value):
            raise ValueError("reference_case_ids must contain positive ids")
        return value


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
    generation_granularity: str = "standard"
    test_dimensions: List[str] = Field(default_factory=list)
    reference_case_count: int = 0
    test_point_count: int = 0
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


class RetrievalCheckpointOut(BaseModel):
    id: int
    task_id: int
    attempt: int
    version: int
    status: str
    auto_review: bool = False
    query: str
    candidate_citations: List[TaskCitationOut] = Field(default_factory=list)
    selected_citation_ids: List[int] = Field(default_factory=list)
    supplemental_text: str = ""
    idempotency_key: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class RetrievalCheckpointConfirm(BaseModel):
    selected_citation_ids: List[int] = Field(default_factory=list)
    supplemental_text: str = Field(default="", max_length=10000)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)


class TaskReferenceCaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    source_case_id: Optional[int] = None
    source_case_key: Optional[str] = None
    title_snapshot: str
    content_md_snapshot: str
    content_hash: str
    source: str
    created_at: datetime


class TestPointInput(BaseModel):
    id: Optional[int] = Field(default=None, ge=1)
    stable_key: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=240)
    verification_goal: str = Field(default="", max_length=1000)
    dimension: str = Field(default="positive", min_length=1, max_length=40)
    priority: Literal["P0", "P1", "P2"] = "P1"
    sort_order: int = Field(default=0, ge=0, le=10000)
    is_selected: bool = True
    is_excluded: bool = False
    citation_ids: List[int] = Field(default_factory=list, max_length=50)


class TestPointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    checkpoint_id: int
    stable_key: str
    title: str
    verification_goal: str
    dimension: str
    priority: str
    sort_order: int
    is_selected: bool
    is_excluded: bool
    citation_ids: List[int] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class TestPointCheckpointOut(BaseModel):
    id: int
    task_id: int
    retrieval_checkpoint_id: Optional[int] = None
    attempt: int
    version: int
    status: str
    points: List[TestPointOut] = Field(default_factory=list)
    idempotency_key: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class TestPointEditRequest(BaseModel):
    points: List[TestPointInput] = Field(max_length=200)
    expected_version: int = Field(ge=1)


class TestPointConfirmRequest(TestPointEditRequest):
    idempotency_key: str = Field(min_length=1, max_length=200)


class RequirementOptimizeRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=20000)
    focus_tags: List[str] = Field(default_factory=list, max_length=30)
    model_id: Optional[int] = Field(default=None, ge=1)


class RequirementOptimizeOut(BaseModel):
    title: str
    description: str
    questions: List[str] = Field(default_factory=list)
    prompt_type: str = "requirement_optimize"


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
    priority: str = "P1"
    revision: int
    source_task_id: Optional[int] = None
    source_draft_id: Optional[int] = None
    source_draft_version: Optional[int] = None
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
    priority: Optional[Literal["P0", "P1", "P2"]] = None


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
    priority: Literal["P0", "P1", "P2"] = "P1"


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


class CoverageTestPointOut(BaseModel):
    stable_key: str
    title: str
    priority: str
    dimension: str
    selected: bool
    excluded: bool
    covered: bool
    case_ids: List[int] = Field(default_factory=list)
    citation_ids: List[int] = Field(default_factory=list)


class CoverageCitationOut(BaseModel):
    citation_id: int
    title: str
    path: str
    test_point_keys: List[str] = Field(default_factory=list)
    case_ids: List[int] = Field(default_factory=list)
    used: bool = False


class CoverageSummaryOut(BaseModel):
    task_id: int
    total_test_points: int
    selected_test_points: int
    covered_test_points: int
    uncovered_test_points: int
    coverage_percent: float
    points: List[CoverageTestPointOut] = Field(default_factory=list)
    citations: List[CoverageCitationOut] = Field(default_factory=list)
