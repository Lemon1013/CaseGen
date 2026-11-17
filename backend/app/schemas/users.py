from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class UserOut(BaseModel):
    """Public user projection.  Never contains the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(default="", max_length=80)
    password: str = Field(min_length=10, max_length=1024)
    role: Literal["admin", "user"] = "user"


class UserUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=80)
    role: Optional[Literal["admin", "user"]] = None
    is_active: Optional[bool] = None


class UserPasswordReset(BaseModel):
    password: str = Field(min_length=10, max_length=1024)
