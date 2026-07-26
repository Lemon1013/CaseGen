from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


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
    created_at: datetime


class RechunkOut(BaseModel):
    document_id: int
    chunk_count: int
