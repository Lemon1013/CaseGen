from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.models.entities import Project
from app.models.external_wiki import ProjectExternalWiki, _utcnow
from app.schemas.external_wiki import (
    ExternalWikiConfigOut,
    ExternalWikiConfigUpdate,
    ExternalWikiTestRequest,
    ExternalWikiTestResponse,
)
from app.services.external_wiki_client import list_external_projects

router = APIRouter(prefix="/api/projects/{project_id}/external-wiki", tags=["external-wiki"])


def _require_admin(request: Request) -> None:
    if not config.AUTH_ENABLED:
        return
    user = getattr(request.state, "user", None)
    if user is None or getattr(user, "role", "") != "admin":
        raise HTTPException(status_code=403, detail="Project management requires an administrator")


def _require_project(request: Request, project_id: int) -> None:
    from app.services.projects import require_request_project
    require_request_project(request, project_id)


def _get_project(session: Session, project_id: int) -> Project:
    row = session.get(Project, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return row


@router.get("", response_model=ExternalWikiConfigOut)
def get_external_wiki_config(
    project_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> ExternalWikiConfigOut:
    _require_project(request, project_id)
    _get_project(session, project_id)
    row = session.exec(
        select(ProjectExternalWiki).where(ProjectExternalWiki.project_id == project_id)
    ).first()
    if row is not None:
        return ExternalWikiConfigOut.model_validate(row)
    # Return default non-persisted configuration
    return ExternalWikiConfigOut(
        project_id=project_id,
        name="外部LLM知识库",
        base_url="http://127.0.0.1:8091",
        external_project_id="",
        external_project_name="",
        top_k=6,
        timeout_sec=3.0,
        weight=1.0,
        use_synonyms=True,
        enabled=True,
    )


@router.put("", response_model=ExternalWikiConfigOut)
def save_external_wiki_config(
    project_id: int,
    payload: ExternalWikiConfigUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> ExternalWikiConfigOut:
    _require_project(request, project_id)
    _require_admin(request)
    _get_project(session, project_id)

    row = session.exec(
        select(ProjectExternalWiki).where(ProjectExternalWiki.project_id == project_id)
    ).first()

    if row is None:
        row = ProjectExternalWiki(
            project_id=project_id,
            name=payload.name.strip() or "外部LLM知识库",
            base_url=payload.base_url.strip(),
            external_project_id=payload.external_project_id.strip(),
            external_project_name=payload.external_project_name.strip(),
            top_k=payload.top_k,
            timeout_sec=payload.timeout_sec,
            weight=payload.weight,
            use_synonyms=payload.use_synonyms,
            enabled=payload.enabled,
        )
        session.add(row)
    else:
        row.name = payload.name.strip() or "外部LLM知识库"
        row.base_url = payload.base_url.strip()
        row.external_project_id = payload.external_project_id.strip()
        row.external_project_name = payload.external_project_name.strip()
        row.top_k = payload.top_k
        row.timeout_sec = payload.timeout_sec
        row.weight = payload.weight
        row.use_synonyms = payload.use_synonyms
        row.enabled = payload.enabled
        row.updated_at = _utcnow()
        session.add(row)

    session.commit()
    session.refresh(row)
    return ExternalWikiConfigOut.model_validate(row)


@router.post("/test-connection", response_model=ExternalWikiTestResponse)
def test_external_connection(
    project_id: int,
    payload: ExternalWikiTestRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> ExternalWikiTestResponse:
    _require_project(request, project_id)
    _require_admin(request)
    _get_project(session, project_id)

    projects = list_external_projects(payload.base_url, timeout_sec=payload.timeout_sec)
    if projects:
        return ExternalWikiTestResponse(
            success=True,
            message=f"连接成功，获取到 {len(projects)} 个外部项目",
            projects=projects,
        )
    return ExternalWikiTestResponse(
        success=False,
        message="连接失败或服务未返回任何项目，请检查 Base URL、网络状态或外部知识库服务运行状态",
        projects=[],
    )
