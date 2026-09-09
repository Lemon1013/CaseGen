from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ExternalWikiConfigUpdate(BaseModel):
    name: str = Field(default="外部LLM知识库", max_length=120)
    base_url: str = Field(default="http://127.0.0.1:8091", max_length=500)
    external_project_id: str = Field(default="", max_length=200)
    external_project_name: str = Field(default="", max_length=200)
    top_k: int = Field(default=6, ge=1, le=50)
    timeout_sec: float = Field(default=3.0, ge=0.5, le=60.0)
    weight: float = Field(default=1.0, ge=0.0, le=10.0)
    use_synonyms: bool = True
    enabled: bool = True


class ExternalWikiConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    project_id: int
    name: str
    base_url: str
    external_project_id: str
    external_project_name: str
    top_k: int
    timeout_sec: float
    weight: float
    use_synonyms: bool
    enabled: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ExternalWikiTestRequest(BaseModel):
    base_url: str = Field(default="http://127.0.0.1:8091", max_length=500)
    timeout_sec: float = Field(default=3.0, ge=0.5, le=60.0)


class ExternalWikiTestResponse(BaseModel):
    success: bool
    message: str = ""
    projects: list[dict[str, Any]] = Field(default_factory=list)
