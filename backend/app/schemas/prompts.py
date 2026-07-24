from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class PromptCreate(BaseModel):
    name: str
    type: str
    content: str
    is_active: bool = True


class PromptUpdate(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None
    is_active: Optional[bool] = None


class PromptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: str
    content: str
    version: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
