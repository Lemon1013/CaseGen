"""Compatibility adapter over ProjectKnowledgeSource for legacy /external-wiki endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from app.db import get_session
from app.models.entities import Project
from app.models.external_wiki import ProjectKnowledgeSource, _utcnow
from app.schemas.external_wiki import (
    ExternalWikiConfigOut,
    ExternalWikiConfigUpdate,
    ExternalWikiTestRequest,
    ExternalWikiTestResponse,
)
from app.services.knowledge_providers import (
    KnowledgeProviderError,
    get_provider,
    parse_json_dict,
    validate_http_base_url,
)
from app.services.permissions import require_admin

router = APIRouter(prefix="/api/projects/{project_id}/external-wiki", tags=["external-wiki"])


def _require_admin(request: Request) -> None:
    require_admin(request, detail="Project management requires an administrator")


def _require_project(request: Request, project_id: int) -> None:
    from app.services.projects import require_request_project

    require_request_project(request, project_id)


def _get_project(session: Session, project_id: int) -> Project:
    row = session.get(Project, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return row


def _first_llm_wiki_source(
    session: Session,
    project_id: int,
) -> ProjectKnowledgeSource | None:
    return session.exec(
        select(ProjectKnowledgeSource)
        .where(
            ProjectKnowledgeSource.project_id == project_id,
            ProjectKnowledgeSource.provider_type == "llm_wiki_django",
        )
        .order_by(ProjectKnowledgeSource.priority.asc(), ProjectKnowledgeSource.id.asc())
    ).first()


def _source_to_legacy_out(row: ProjectKnowledgeSource) -> ExternalWikiConfigOut:
    cfg = parse_json_dict(row.config_json)
    return ExternalWikiConfigOut(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        base_url=row.base_url,
        external_project_id=str(cfg.get("external_project_id") or ""),
        external_project_name=str(cfg.get("external_project_name") or ""),
        top_k=int(cfg.get("top_k") or 6),
        timeout_sec=float(cfg.get("timeout_sec") if cfg.get("timeout_sec") is not None else 3.0),
        weight=float(row.weight if row.weight is not None else 1.0),
        use_synonyms=bool(cfg.get("use_synonyms", True)),
        enabled=bool(row.enabled),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=ExternalWikiConfigOut)
def get_external_wiki_config(
    project_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> ExternalWikiConfigOut:
    _require_project(request, project_id)
    _get_project(session, project_id)
    row = _first_llm_wiki_source(session, project_id)
    if row is not None:
        return _source_to_legacy_out(row)
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

    try:
        clean_url = validate_http_base_url(payload.base_url)
    except KnowledgeProviderError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc

    row = _first_llm_wiki_source(session, project_id)
    cfg_payload = {
        "external_project_id": payload.external_project_id.strip(),
        "external_project_name": payload.external_project_name.strip(),
        "top_k": payload.top_k,
        "timeout_sec": payload.timeout_sec,
        "use_synonyms": payload.use_synonyms,
    }
    if row is None:
        row = ProjectKnowledgeSource(
            project_id=project_id,
            name=payload.name.strip() or "外部LLM知识库",
            provider_type="llm_wiki_django",
            base_url=clean_url,
            auth_type="none",
            config_json=json.dumps(cfg_payload, ensure_ascii=False),
            credentials_json="{}",
            weight=payload.weight,
            priority=100,
            enabled=payload.enabled,
        )
    else:
        existing_cfg = parse_json_dict(row.config_json)
        existing_cfg.update(cfg_payload)
        row.name = payload.name.strip() or "外部LLM知识库"
        row.base_url = clean_url
        row.config_json = json.dumps(existing_cfg, ensure_ascii=False)
        row.weight = payload.weight
        row.enabled = payload.enabled
        row.updated_at = _utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return _source_to_legacy_out(row)


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

    try:
        clean_url = validate_http_base_url(payload.base_url)
    except KnowledgeProviderError as exc:
        return ExternalWikiTestResponse(success=False, message=exc.message, projects=[])

    existing = _first_llm_wiki_source(session, project_id)
    temp_source = ProjectKnowledgeSource(
        id=existing.id if existing is not None else None,
        project_id=project_id,
        name="连接测试",
        provider_type="llm_wiki_django",
        base_url=clean_url,
        auth_type=existing.auth_type if existing is not None else "none",
        config_json=json.dumps({"timeout_sec": payload.timeout_sec}, ensure_ascii=False),
        credentials_json=existing.credentials_json if existing is not None else "{}",
        enabled=True,
    )
    provider = get_provider("llm_wiki_django")
    try:
        result = provider.test_connection(temp_source)
    except KnowledgeProviderError as exc:
        return ExternalWikiTestResponse(
            success=False,
            message=exc.message,
            projects=[],
        )
    scopes = result.get("scopes") or []
    projects = [
        {"id": scope.id, "name": scope.name, "path": scope.path}
        if hasattr(scope, "id")
        else dict(scope)
        for scope in scopes
    ]
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
