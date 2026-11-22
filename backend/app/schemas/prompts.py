from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

PromptVisibility = Literal["system", "shared", "private"]
ForkVisibility = Literal["private", "shared"]


class PromptCreate(BaseModel):
    name: str
    type: str
    content: str
    is_active: bool = True
    visibility: Optional[PromptVisibility] = None


class PromptUpdate(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None
    is_active: Optional[bool] = None
    visibility: Optional[PromptVisibility] = None


class PromptForkRequest(BaseModel):
    name: Optional[str] = None
    visibility: ForkVisibility = "private"


class PromptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: str
    content: str
    version: int
    is_active: bool
    owner_user_id: Optional[int] = None
    owner_username: Optional[str] = None
    visibility: str = "system"
    can_edit: bool = False
    can_delete: bool = False
    can_fork: bool = False
    created_at: datetime
    updated_at: datetime
