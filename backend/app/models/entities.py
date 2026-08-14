from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Index, UniqueConstraint, text
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

    __table_args__ = (
        # Only one model may be marked as the runtime default.  SQLite's
        # partial unique index still permits any number of non-default models.
        Index(
            "uq_models_single_default",
            "is_default",
            unique=True,
            sqlite_where=text("is_default = 1"),
        ),
    )


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


class WikiSpace(SQLModel, table=True):
    """Project-scoped Wiki namespace."""

    __tablename__ = "wiki_spaces"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    slug: str = Field(index=True, unique=True)
    description: str = ""
    status: str = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})

    __table_args__ = (
        Index("ix_wiki_spaces_status_updated", "status", "updated_at"),
    )


class Document(SQLModel, table=True):
    __tablename__ = "documents"

    id: Optional[int] = Field(default=None, primary_key=True)
    filename: str
    stored_path: str
    content_type: str
    sha256: str
    status: str
    # Nullable for legacy fixtures; services resolve it to the default space.
    space_id: Optional[int] = Field(default=None, foreign_key="wiki_spaces.id", index=True)
    char_count: int = 0
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})


class IngestJob(SQLModel, table=True):
    __tablename__ = "ingest_jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(index=True)
    space_id: Optional[int] = Field(default=None, foreign_key="wiki_spaces.id", index=True)
    status: str
    # Wiki 2.0 uses stage for the durable, user-visible pipeline phase while
    # status remains for compatibility with the original ingest API.
    stage: str = Field(default="queued", index=True)
    progress: int = Field(default=0)
    plan_json: str = "{}"
    model_ref: Optional[str] = None
    prompt_version_ref: Optional[str] = None
    cancel_requested: bool = False
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
    space_id: Optional[int] = Field(default=None, foreign_key="wiki_spaces.id", index=True)
    tags_json: str = "[]"
    # Optional during the migration window because the old synchronous
    # ingest path still creates rows without a page_key.
    page_key: Optional[str] = None
    domain: Optional[str] = Field(default=None, index=True)
    status: str = Field(default="published", index=True)
    revision: int = 1
    aliases_json: str = "[]"
    content_hash: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})

    __table_args__ = (
        # SQLite permits multiple NULL values in a unique index.  The partial
        # predicate makes the migration contract explicit and leaves legacy
        # rows insertable until the migration backfills their keys.
        Index(
            "uq_wiki_pages_page_key_not_null",
            "page_key",
            unique=False,
        ),
        Index(
            "uq_wiki_pages_space_page_key",
            "space_id",
            "page_key",
            unique=True,
            sqlite_where=text("page_key IS NOT NULL"),
        ),
        Index("ix_wiki_pages_space_status", "space_id", "status"),
        Index("ix_wiki_pages_space_document", "space_id", "source_document_id"),
    )


class WikiPageSource(SQLModel, table=True):
    """Traceable many-to-many link between a Wiki page and a source document."""

    __tablename__ = "wiki_page_sources"

    id: Optional[int] = Field(default=None, primary_key=True)
    page_id: int = Field(foreign_key="wiki_pages.id", index=True)
    document_id: int = Field(foreign_key="documents.id", index=True)
    # JSON arrays keep the evidence contract compatible with the existing
    # source chunk and clause representations without coupling Task 2 to a
    # future normalized anchor table.
    chunk_ids_json: str = "[]"
    clauses_json: str = "[]"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})

    __table_args__ = (
        UniqueConstraint(
            "page_id",
            "document_id",
            name="uq_wiki_page_sources_page_document",
        ),
        Index(
            "ix_wiki_page_sources_document_page",
            "document_id",
            "page_id",
        ),
    )


class WikiPageRevision(SQLModel, table=True):
    """Immutable Wiki page snapshot created by a future apply/review step."""

    __tablename__ = "wiki_page_revisions"

    id: Optional[int] = Field(default=None, primary_key=True)
    page_id: int = Field(foreign_key="wiki_pages.id", index=True)
    revision: int
    # Serialized frontmatter and Markdown body are intentionally stored as
    # snapshots, so a revision can be rendered even if the file changes.
    frontmatter_json: str = "{}"
    content_md: str = ""
    operation: str = "update"
    job_id: Optional[int] = Field(default=None, foreign_key="ingest_jobs.id", index=True)
    reason: str = ""
    created_at: datetime = Field(default_factory=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "page_id",
            "revision",
            name="uq_wiki_page_revisions_page_revision",
        ),
    )


class WikiReviewItem(SQLModel, table=True):
    """Auditable candidate change requiring an explicit review decision."""

    __tablename__ = "wiki_review_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    page_id: Optional[int] = Field(default=None, foreign_key="wiki_pages.id", index=True)
    job_id: Optional[int] = Field(default=None, foreign_key="ingest_jobs.id", index=True)
    space_id: Optional[int] = Field(default=None, foreign_key="wiki_spaces.id", index=True)
    kind: str = "conflict"
    status: str = Field(default="pending", index=True)
    reason: str = ""
    # Candidate content is optional: some review items only describe a
    # conflict or high-risk claim and do not yet contain a full page.
    candidate_frontmatter_json: Optional[str] = None
    candidate_content_md: Optional[str] = None
    payload_json: str = "{}"
    reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    decision_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})

    __table_args__ = (
        Index("ix_wiki_review_items_page_status", "page_id", "status"),
        Index("ix_wiki_review_items_job_status", "job_id", "status"),
        Index("ix_wiki_review_items_space_status", "space_id", "status"),
    )


class SourceChunk(SQLModel, table=True):
    """Verbatim source text slice — lossless layer for hybrid retrieve."""

    __tablename__ = "source_chunks"

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(index=True)
    space_id: Optional[int] = Field(default=None, foreign_key="wiki_spaces.id", index=True)
    chunk_index: int = 0
    title: str = ""
    text: str = ""
    start_char: int = 0
    end_char: int = 0
    # Optional source anchors keep old rows and callers valid while allowing
    # new chunks to point back to PDF pages, headings, and parent paragraphs.
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    section: str = ""
    clause_ids_json: str = "[]"
    parent_index: Optional[int] = None
    created_at: datetime = Field(default_factory=_utcnow)


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
    wiki_space_id: Optional[int] = Field(default=None, foreign_key="wiki_spaces.id", index=True)
    status: str
    model_id: Optional[int] = None
    review_model_id: Optional[int] = None
    prompt_template_id: Optional[int] = None
    temp_prompt_content: Optional[str] = None
    error_message: Optional[str] = None
    # The exact draft selected for final import.  These fields were added
    # after the original task workflow; they remain nullable so old rows and
    # the compatibility POST /finalize contract continue to work.
    finalized_draft_id: Optional[int] = Field(default=None, foreign_key="case_drafts.id", index=True)
    finalized_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})


class TaskCitation(SQLModel, table=True):
    __tablename__ = "task_citations"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int
    wiki_page_id: Optional[int] = None
    # wiki | source
    citation_type: str = "wiki"
    source_chunk_id: Optional[int] = None
    title: str
    path: str
    score: float = 0.0
    snippet: str = ""
    # Longer excerpt for source citations (UI drawer / generate context)
    content_excerpt: str = ""
    # JSON list of clause ids e.g. ["3.5.1","3.5.2"]
    clause_ids_json: str = "[]"
    # Primary anchored clause if any
    anchor_clause: Optional[str] = None


class TaskRetrievalCheckpoint(SQLModel, table=True):
    """Durable evidence snapshot awaiting an explicit generation decision."""

    __tablename__ = "task_retrieval_checkpoints"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="generation_tasks.id", index=True)
    attempt: int = Field(default=1, index=True)
    version: int = Field(default=1, index=True)
    status: str = Field(default="pending", index=True)
    auto_review: bool = False
    resume_claim_token: Optional[str] = None
    resume_claimed_at: Optional[datetime] = None
    resume_started_at: Optional[datetime] = None
    resume_status: Optional[str] = None
    query: str = ""
    retrieval_json: str = "{}"
    candidate_citation_ids_json: str = "[]"
    selected_citation_ids_json: str = "[]"
    supplemental_text: str = ""
    decision_hash: Optional[str] = None
    idempotency_key: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})

    __table_args__ = (
        UniqueConstraint("task_id", "attempt", name="uq_task_retrieval_checkpoint_attempt"),
        Index("ix_task_retrieval_checkpoint_task_status", "task_id", "status"),
    )


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


class TestCase(SQLModel, table=True):
    """The current, editable state of an imported test case.

    Case history/rollback intentionally is not modelled here.  Every edit is
    instead captured as a compact :class:`TestCaseOperationLog` row while this
    table stores only the latest content.
    """

    __tablename__ = "test_cases"

    id: Optional[int] = Field(default=None, primary_key=True)
    requirement_id: int = Field(foreign_key="requirements.id", index=True)
    case_key: str = Field(index=True)
    title: str = ""
    content_md: str = ""
    status: str = Field(default="active", index=True)
    revision: int = Field(default=1)
    # Source references are intentionally not foreign keys: deleting a task
    # must not cascade into already-imported current-state cases.
    source_task_id: Optional[int] = Field(default=None, index=True)
    source_draft_id: Optional[int] = Field(default=None, index=True)
    source_case_key: Optional[str] = Field(default=None, index=True)
    archived_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})

    __table_args__ = (
        Index(
            "uq_test_cases_source_identity",
            "source_task_id",
            "source_draft_id",
            "source_case_key",
            unique=True,
        ),
        Index("ix_test_cases_requirement_status_key", "requirement_id", "status", "case_key"),
        # The normalized requirement/key uniqueness index is installed by the
        # compatibility migration after it checks legacy duplicates.  Keeping
        # this non-unique index in metadata lets an old database start up so a
        # diagnostic warning can be emitted instead of an opaque create_all
        # IntegrityError.
        Index("ix_test_cases_requirement_case_key", "requirement_id", "case_key"),
    )


class TestCaseOperationLog(SQLModel, table=True):
    """Non-reversible audit metadata for import, edit and lifecycle actions.

    Older local databases may still have required ``diff_text``/``diff_json``
    columns.  They remain mapped only so inserts can provide irreversible
    empty placeholders; the application never stores or returns body/diff
    content in them.  New rows contain only hashes, lengths, line counts and
    field names, so an operation log cannot reconstruct an earlier case body.
    """

    __tablename__ = "test_case_operation_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    test_case_id: int = Field(foreign_key="test_cases.id", index=True)
    operation: str = Field(index=True)
    # Compatibility-only columns for the pre-release SQLite schema.  Always
    # empty: keeping the NOT NULL contract must not reintroduce case history.
    diff_text: str = ""
    diff_json: str = "{}"
    changed_fields_json: str = "[]"
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
    created_at: datetime = Field(default_factory=_utcnow)


class User(SQLModel, table=True):
    """Local CaseGen account; this release supports one initial admin."""

    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    display_name: str = ""
    password_hash: str
    role: str = "admin"
    is_active: bool = True
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})


class AuthSession(SQLModel, table=True):
    """Hashed bearer token persisted for cookie-backed browser sessions."""

    __tablename__ = "auth_sessions"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    token_hash: str = Field(unique=True, index=True)
    expires_at: datetime = Field(index=True)
    last_seen_at: datetime = Field(default_factory=_utcnow)
    created_at: datetime = Field(default_factory=_utcnow)
    user_agent: Optional[str] = None
    ip: Optional[str] = None
