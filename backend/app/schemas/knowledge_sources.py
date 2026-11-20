"""Pydantic request and response schemas for multi-provider knowledge source APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.services.knowledge_providers.base import RemoteScope


class KnowledgeSourceCreate(BaseModel):
    name: str = Field(default="外部知识库", max_length=120)
    provider_type: str = Field(default="llm_wiki_django", max_length=64)
    base_url: str = Field(default="http://127.0.0.1:8091", max_length=500)
    auth_type: str = Field(default="none", max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, Any] = Field(default_factory=dict)
    weight: float = Field(default=1.0, ge=0.0, le=10.0)
    priority: int = Field(default=100, ge=0, le=10000)
    enabled: bool = True


class KnowledgeSourceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    provider_type: Optional[str] = Field(default=None, max_length=64)
    base_url: Optional[str] = Field(default=None, max_length=500)
    auth_type: Optional[str] = Field(default=None, max_length=32)
    config: Optional[dict[str, Any]] = None
    credentials: Optional[dict[str, Any]] = None
    weight: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    priority: Optional[int] = Field(default=None, ge=0, le=10000)
    enabled: Optional[bool] = None


class KnowledgeSourceOut(BaseModel):
    id: int
    project_id: int
    name: str
    provider_type: str
    base_url: str
    auth_type: str
    config: dict[str, Any] = Field(default_factory=dict)
    has_credentials: bool = False
    auth_username: str = ""
    weight: float
    priority: int
    enabled: bool
    created_at: datetime
    updated_at: datetime


class KnowledgeSourceTestRequest(BaseModel):
    source_id: Optional[int] = None
    provider_type: str = Field(default="llm_wiki_django", max_length=64)
    base_url: str = Field(default="http://127.0.0.1:8091", max_length=500)
    auth_type: str = Field(default="none", max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSourceTestResponse(BaseModel):
    ok: bool
    message: str = ""
    scopes: list[RemoteScope] = Field(default_factory=list)
