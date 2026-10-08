from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    # Naive UTC so SQLite round-trips stay comparable without tzinfo.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ProjectExternalWiki(SQLModel, table=True):
    __tablename__ = "project_external_wikis"

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True, unique=True)
    name: str = "外部LLM知识库"
    base_url: str = "http://127.0.0.1:8091"
    external_project_id: str = ""
    external_project_name: str = ""
    top_k: int = 6
    timeout_sec: float = 3.0
    weight: float = 1.0
    use_synonyms: bool = True
    enabled: bool = True
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column_kwargs={"onupdate": _utcnow})
