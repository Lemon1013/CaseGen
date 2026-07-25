from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class RequirementCreate(BaseModel):
    title: str
    description: str
    focus_tags: List[str] = Field(default_factory=list)


class RequirementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str
    focus_tags: List[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
