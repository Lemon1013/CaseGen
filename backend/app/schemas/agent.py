"""Pydantic schemas for the DSH agent chat API.

Field names stay snake_case on both ends, matching the rest of the backend.
The draft payload models mirror the app.schemas.tasks draft output models so
the agent's save-as-draft exit reuses the existing structured draft UI.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentConversationCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    skill_name: Optional[str] = Field(default=None, max_length=120)
    model: Optional[str] = Field(default=None, max_length=200)


class AgentConversationUpdate(BaseModel):
    """Rename / re-select skill or model for an existing conversation."""

    title: Optional[str] = Field(default=None, max_length=200)
    skill_name: Optional[str] = Field(default=None, max_length=120)
    model: Optional[str] = Field(default=None, max_length=200)
    expected_version: Optional[int] = Field(default=None, ge=1)


class AgentConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    title: str
    dsh_session_id: Optional[str] = None
    skill_name: Optional[str] = None
    model: Optional[str] = None
    workspace_dir: str = ""
    status: str
    cancel_requested: bool = False
    version: int = 1
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


class AgentMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    role: str
    content: str
    reasoning: str = ""
    created_at: datetime


class AgentConversationDetail(AgentConversationOut):
    """Flat conversation plus its message history (the durable polling source)."""

    messages: List[AgentMessageOut] = Field(default_factory=list)


class AgentMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=32_000)
    idempotency_key: str = Field(min_length=1, max_length=200)
    # Optional optimistic lock; when supplied a stale version yields 409.
    expected_version: Optional[int] = Field(default=None, ge=1)


class AgentMessageAccepted(BaseModel):
    """Response of POST /api/agent/conversations/{id}/messages.

    The turn runs in a background thread, so the reply names the persisted user
    message and the current conversation state rather than an assistant answer.
    """

    conversation: AgentConversationOut
    message: AgentMessageOut
    duplicated: bool = False


class AgentCancelOut(BaseModel):
    conversation: AgentConversationOut
    cancelled: bool = True
    message: str = ""


class AgentDeleteOut(BaseModel):
    id: str
    status: str
    message: str = ""


class AgentSkillOut(BaseModel):
    name: str
    description: str = ""
    # Repository-relative path of the SKILL.md file; debugging aid only.
    path: str = ""


class AgentDraftCaseStepIn(BaseModel):
    """One step of an agent-produced case (mirrors DraftCaseStepOut)."""

    step_no: int = 0
    action: str = ""
    expected: str = ""


class AgentDraftCaseIn(BaseModel):
    """Structured case emitted by the agent (mirrors DraftCaseItemOut).

    raw_md is accepted for contract compatibility but is never re-emitted:
    the durable Markdown is rendered from the structured fields only, so an
    answer cannot inject an extra "## TC-xxx" heading that the importer would
    treat as another case.
    """

    case_key: str = ""
    title: str = ""
    priority: str = "P1"
    type: str = ""
    verification_goal: str = ""
    preconditions: str = ""
    test_data: str = ""
    steps: List[AgentDraftCaseStepIn] = Field(default_factory=list)
    pending_items: List[str] = Field(default_factory=list)
    raw_md: str = ""


class AgentDraftPointGroupIn(BaseModel):
    """Test-point group (mirrors DraftPointGroupOut, defaults relaxed)."""

    test_point_id: Optional[int] = None
    stable_key: str = ""
    title: str = ""
    verification_goal: str = ""
    dimension: str = ""
    priority: str = "P1"
    citation_ids: List[int] = Field(default_factory=list)
    cases: List[AgentDraftCaseIn] = Field(default_factory=list)


class AgentSaveAsDraftOut(BaseModel):
    task_id: int
    draft_id: int
    requirement_id: int
    imported_count: int
    case_keys: List[str] = Field(default_factory=list)
    version: int = 1
    message: str = ""
