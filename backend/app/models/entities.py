from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    # Naive UTC so SQLite round-trips stay comparable without tzinfo.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ModelConfig(SQLModel, table=True):
    __tablename__ = "models"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    base_url: str
    api_key: str
    model_name: str
    is_default: bool = False
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})


class PromptTemplate(SQLModel, table=True):
    __tablename__ = "prompt_templates"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    type: str
    content: str
    version: int = 1
    is_active: bool = True
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})


class Document(SQLModel, table=True):
    __tablename__ = "documents"

    id: Optional[int] = Field(default=None, primary_key=True)
    filename: str
    stored_path: str
    content_type: str
    sha256: str
    status: str
    char_count: int = 0
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})


class IngestJob(SQLModel, table=True):
    __tablename__ = "ingest_jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int
    status: str
    step_log_json: str = "[]"
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})


class WikiPageRow(SQLModel, table=True):
    __tablename__ = "wiki_pages"

    id: Optional[int] = Field(default=None, primary_key=True)
    path: str
    title: str
    page_type: str
    source_document_id: Optional[int] = None
    tags_json: str = "[]"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})


class Requirement(SQLModel, table=True):
    __tablename__ = "requirements"

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    description: str
    focus_tags_json: str = "[]"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})


class GenerationTask(SQLModel, table=True):
    __tablename__ = "generation_tasks"

    id: Optional[int] = Field(default=None, primary_key=True)
    requirement_id: int
    status: str
    model_id: Optional[int] = None
    review_model_id: Optional[int] = None
    prompt_template_id: Optional[int] = None
    temp_prompt_content: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})


class TaskCitation(SQLModel, table=True):
    __tablename__ = "task_citations"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int
    wiki_page_id: Optional[int] = None
    title: str
    path: str
    score: float = 0.0
    snippet: str = ""


class CaseDraft(SQLModel, table=True):
    __tablename__ = "case_drafts"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int
    version: int = 1
    content_md: str
    prompt_version_ref: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)


class ReviewResult(SQLModel, table=True):
    __tablename__ = "review_results"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int
    draft_id: int
    score: int
    verdict: str
    payload_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)


class PromptRevision(SQLModel, table=True):
    __tablename__ = "prompt_revisions"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int
    base_prompt_id: Optional[int] = None
    new_content: str
    status: str
    created_at: datetime = Field(default_factory=_utcnow)


class TaskEvent(SQLModel, table=True):
    __tablename__ = "task_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int
    step: str
    message: str
    detail_json: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
