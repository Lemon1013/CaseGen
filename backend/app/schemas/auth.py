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
    registration_enabled: bool = False


class SetupBody(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(default="", max_length=80)
    password: str = Field(min_length=10, max_length=1024)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class RegisterBody(BaseModel):
    """Anonymous self-registration payload.

    Deliberately has no ``role``/``is_active`` fields; extra JSON keys are
    ignored by Pydantic and can never influence the created account.
    """

    username: str = Field(max_length=32)
    display_name: str = Field(default="", max_length=80)
    password: str = Field(max_length=1024)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(max_length=1024)


class RegistrationSettingIn(BaseModel):
    enabled: bool


class RegistrationSettingOut(BaseModel):
    enabled: bool


class AuthSessionOut(BaseModel):
    user: AuthUserOut
    expires_at: datetime
