from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class IngestJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    status: str
    step_log_json: str
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class WikiPageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    path: str
    title: str
    page_type: str
    source_document_id: Optional[int] = None
    tags: List[str] = Field(default_factory=list)
    content: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class WikiIndexOut(BaseModel):
    content: str
    path: str = "wiki/index.md"


class RetrieveRequest(BaseModel):
    query: str
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


class RetrieveResponse(BaseModel):
    query: str
    hits: List[RetrieveHit]
    wiki_hit_count: int = 0
    source_hit_count: int = 0
    clause_ids: List[str] = Field(default_factory=list)
    anchored_clause_ids: List[str] = Field(default_factory=list)
