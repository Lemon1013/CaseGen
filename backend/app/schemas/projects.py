from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: Optional[str] = Field(default=None, max_length=64)
    description: str = Field(default="", max_length=2000)


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    status: Optional[str] = None


class ProjectBindingIn(BaseModel):
    wiki_space_id: int = Field(ge=1)
    priority: int = Field(default=100, ge=0, le=10000)
    enabled: bool = True


class ProjectSpaceOut(BaseModel):
    id: int
    name: str
    slug: str
    scope: str
    namespace: Optional[str] = None
    status: str
    priority: Optional[int] = None
    enabled: bool = True


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    description: str
    status: str
    default_wiki_space_id: Optional[int] = None
    private_spaces: list[ProjectSpaceOut] = Field(default_factory=list)
    shared_spaces: list[ProjectSpaceOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
