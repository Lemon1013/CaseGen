from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IngestJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    space_id: int
    space_name: str = ""
    status: str
    stage: str = "queued"
    progress: int = 0
    plan_json: str = "{}"
    cancel_requested: bool = False
    model_ref: Optional[str] = None
    prompt_version_ref: Optional[str] = None
    step_log_json: str
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class WikiPageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    path: str
    space_id: int
    space_name: str = ""
    title: str
    page_type: str
    source_document_id: Optional[int] = None
    page_key: Optional[str] = None
    domain: Optional[str] = None
    status: str = "published"
    revision: int = 1
    aliases: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    content: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class WikiIndexOut(BaseModel):
    content: str
    path: str = "wiki/index.md"


class RetrieveRequest(BaseModel):
    query: str
    space_id: Optional[int] = Field(default=None, ge=1)
    top_k: Optional[int] = None
    types: Optional[List[str]] = None


class RetrieveHit(BaseModel):
    id: Optional[int] = None
    title: str
    page_type: str
    path: str
    score: float
    snippet: str
    tags: List[str] = Field(default_factory=list)
    content: Optional[str] = None
    source_document_id: Optional[int] = None
    # wiki | source
    citation_type: str = "wiki"
    source_chunk_id: Optional[int] = None
    start_char: Optional[int] = None
    end_char: Optional[int] = None
    clause_ids: List[str] = Field(default_factory=list)
    anchor_clause: Optional[str] = None
    explain: Optional[dict] = None
    page_key: Optional[str] = None
    domain: Optional[str] = None
    status: Optional[str] = None
    revision: Optional[int] = None
    aliases: List[str] = Field(default_factory=list)
    source_document_ids: List[int] = Field(default_factory=list)
    space_id: Optional[int] = None


class RetrieveResponse(BaseModel):
    query: str
    hits: List[RetrieveHit]
    wiki_hit_count: int = 0
    source_hit_count: int = 0
    clause_ids: List[str] = Field(default_factory=list)
    anchored_clause_ids: List[str] = Field(default_factory=list)
    retrieval_mode: Optional[str] = None
    explain: Optional[dict] = None


class WikiSourceEvidenceOut(BaseModel):
    document_id: int
    chunk_ids: List[int] = Field(default_factory=list)
    clauses: List[str] = Field(default_factory=list)


class WikiDiffOut(BaseModel):
    from_revision: Optional[int] = None
    to_revision: Optional[int] = None
    unified: str = ""
    # ``text`` is a readable alias for clients that do not need to know the
    # diff format; ``unified`` remains the canonical field.
    text: str = ""
    changed: bool = False
    available: bool = True
    reason: str = ""


class WikiRevisionOut(BaseModel):
    id: int
    page_id: int
    revision: int
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    frontmatter_json: str = "{}"
    content_md: str = ""
    operation: str = "update"
    job_id: Optional[int] = None
    reason: str = ""
    created_at: datetime


class WikiReviewOut(BaseModel):
    id: int
    page_id: Optional[int] = None
    job_id: Optional[int] = None
    space_id: Optional[int] = None
    space_name: str = ""
    kind: str
    status: str
    reason: str
    candidate_available: bool = False
    reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    decision_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class WikiReviewReasonOut(BaseModel):
    summary: str = ""
    kind: str = ""
    operation: Optional[str] = None
    page_key: Optional[str] = None
    risk_flags: List[str] = Field(default_factory=list)


class WikiCandidateOut(BaseModel):
    page_key: Optional[str] = None
    title: Optional[str] = None
    type: Optional[str] = None
    domain: Optional[str] = None
    aliases: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    status: Optional[str] = None
    sources: List[WikiSourceEvidenceOut] = Field(default_factory=list)
    content_md: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)


class WikiReviewDetailOut(WikiReviewOut):
    old_version: Optional[WikiRevisionOut] = None
    new_candidate: WikiCandidateOut = Field(default_factory=WikiCandidateOut)
    reason_detail: WikiReviewReasonOut = Field(default_factory=WikiReviewReasonOut)
    payload: dict[str, Any] = Field(default_factory=dict)
    source_evidence: List[WikiSourceEvidenceOut] = Field(default_factory=list)
    diff: WikiDiffOut = Field(default_factory=WikiDiffOut)


class WikiReviewDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewed_by: Optional[str] = None
    reason: Optional[str] = None
    decision_reason: Optional[str] = None


class WikiRollbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: Optional[int] = Field(default=None, ge=1)
    revision: Optional[int] = Field(default=None, ge=1)
    job_id: Optional[int] = Field(default=None, ge=1)
    reason: str = Field(default="rollback", min_length=1)
    reviewed_by: Optional[str] = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "WikiRollbackIn":
        if (self.revision_id is None) == (self.revision is None):
            raise ValueError("provide exactly one of revision_id or revision")
        return self


class WikiRollbackOut(WikiRevisionOut):
    pass
