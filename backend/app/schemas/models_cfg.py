from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ModelCreate(BaseModel):
    name: str
    base_url: str
    api_key: str
    model_name: str
    is_default: bool = False


class ModelUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model_name: Optional[str] = None
    is_default: Optional[bool] = None


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    base_url: str
    api_key: str
    model_name: str
    is_default: bool
    created_at: datetime
    updated_at: datetime


class ModelPingOut(BaseModel):
    ok: bool = True
    content: str = Field(default="")
