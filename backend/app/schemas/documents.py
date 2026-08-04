from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    stored_path: str
    content_type: str
    sha256: str
    status: str
    char_count: int
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class SourceChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    chunk_index: int
    title: str
    text: str
    start_char: int
    end_char: int
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    section: str = ""
    clause_ids_json: str = "[]"
    parent_index: Optional[int] = None
    created_at: datetime


class RechunkOut(BaseModel):
    document_id: int
    chunk_count: int


class DocumentPreviewOut(BaseModel):
    document_id: int
    filename: str
    text: str
    char_count: int
    returned_chars: int
    truncated: bool = False
    quality_ok: bool = True
    diagnostics: dict[str, Any] = Field(default_factory=dict)
