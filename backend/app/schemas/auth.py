from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class AuthUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    role: str


class BootstrapOut(BaseModel):
    setup_required: bool
    user: Optional[AuthUserOut] = None


class SetupBody(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(default="", max_length=80)
    password: str = Field(min_length=10, max_length=1024)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class AuthSessionOut(BaseModel):
    user: AuthUserOut
    expires_at: datetime
