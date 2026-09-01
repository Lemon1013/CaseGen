from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class RequirementCreate(BaseModel):
    project_id: Optional[int] = Field(default=None, ge=1)
    title: str
    description: str
    focus_tags: List[str] = Field(default_factory=list)


class RequirementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: Optional[int] = None
    title: str
    description: str
    focus_tags: List[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
