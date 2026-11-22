"""API routes for pluggable external knowledge sources and file proxies."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlmodel import Session, select

from app.db import get_session
from app.models.entities import Project
from app.models.external_wiki import ProjectKnowledgeSource, _utcnow
from app.schemas.knowledge_sources import (
    KnowledgeSourceCreate,
    KnowledgeSourceOut,
    KnowledgeSourceTestRequest,
    KnowledgeSourceTestResponse,
    KnowledgeSourceUpdate,
)
from app.services.knowledge_providers import (
    KnowledgeProviderError,
    ProviderTypeInfo,
    RemoteFileContent,
    RemoteFileItem,
    get_provider,
    list_provider_types,
    list_source_files,
    mask_credentials,
    parse_json_dict,
    read_source_file,
    validate_http_base_url,
)
from app.services.permissions import require_admin
from app.services.projects import require_request_project

router = APIRouter(tags=["knowledge-sources"])

_SECRET_CREDENTIAL_KEYS = ("password", "token", "api_key", "key")


def _require_admin(request: Request) -> None:
    require_admin(request, detail="Project management requires an administrator")


def _get_project(session: Session, project_id: int) -> Project:
    row = session.get(Project, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return row


def _get_source(session: Session, project_id: int, source_id: int) -> ProjectKnowledgeSource:
    row = session.get(ProjectKnowledgeSource, source_id)
    if row is None or int(row.project_id) != int(project_id):
        raise HTTPException(status_code=404, detail="Knowledge source not found")
    return row


def _validate_provider_and_auth(
    provider_type: str,
    auth_type: str,
    base_url: str,
) -> tuple[Any, str, str]:
    try:
        provider = get_provider(provider_type)
    except KnowledgeProviderError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc

    normalized_auth = str(auth_type or "none").strip().lower()
    supported = provider.info().capabilities.supported_auth_types
    if normalized_auth not in supported:
        raise HTTPException(
            status_code=422,
            detail=f"该知识库类型不支持认证方式: {normalized_auth}",
        )
    try:
        normalized_url = validate_http_base_url(base_url)
    except KnowledgeProviderError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    return provider, normalized_auth, normalized_url


def _sanitize_credentials_for_store(
    auth_type: str,
    incoming: dict[str, Any] | None,
    existing_raw: str | None = None,
) -> dict[str, Any]:
    if auth_type == "none":
        return {}
    existing = parse_json_dict(existing_raw) if existing_raw else {}
    if incoming is None:
        return existing

    merged: dict[str, Any] = dict(existing)
    for key, value in incoming.items():
        if key in _SECRET_CREDENTIAL_KEYS:
            if value is not None and str(value) != "":
                merged[key] = str(value)
        else:
            if value is not None:
                merged[key] = str(value).strip() if isinstance(value, str) else value
    return merged


def _to_out(row: ProjectKnowledgeSource) -> KnowledgeSourceOut:
    creds_meta = mask_credentials(row.auth_type, row.credentials_json)
    return KnowledgeSourceOut(
        id=int(row.id or 0),
        project_id=int(row.project_id),
        name=row.name,
        provider_type=row.provider_type,
        base_url=row.base_url,
        auth_type=row.auth_type,
        config=parse_json_dict(row.config_json),
        has_credentials=bool(creds_meta["has_credentials"]),
        auth_username=str(creds_meta["username"] or ""),
        weight=float(row.weight),
        priority=int(row.priority),
        enabled=bool(row.enabled),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _map_provider_error(exc: KnowledgeProviderError) -> HTTPException:
    if exc.category in {"invalid_config", "unsupported"} or exc.status_code == 400:
        return HTTPException(status_code=400, detail=exc.message)
    if exc.status_code == 404:
        return HTTPException(status_code=404, detail=exc.message)
    return HTTPException(status_code=502, detail=exc.message)


@router.get("/api/knowledge-providers/types", response_model=list[ProviderTypeInfo])
def get_knowledge_provider_types() -> list[ProviderTypeInfo]:
    return list_provider_types()


@router.get(
    "/api/projects/{project_id}/knowledge-sources",
    response_model=list[KnowledgeSourceOut],
)
def list_knowledge_sources(
    project_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> list[KnowledgeSourceOut]:
    require_request_project(request, project_id)
    _get_project(session, project_id)
    rows = session.exec(
        select(ProjectKnowledgeSource)
        .where(ProjectKnowledgeSource.project_id == project_id)
        .order_by(ProjectKnowledgeSource.priority.asc(), ProjectKnowledgeSource.id.asc())
    ).all()
    return [_to_out(row) for row in rows]


@router.post(
    "/api/projects/{project_id}/knowledge-sources",
    response_model=KnowledgeSourceOut,
    status_code=status.HTTP_201_CREATED,
)
def create_knowledge_source(
    project_id: int,
    payload: KnowledgeSourceCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> KnowledgeSourceOut:
    require_request_project(request, project_id)
    _require_admin(request)
    _get_project(session, project_id)

    _, normalized_auth, normalized_url = _validate_provider_and_auth(
        payload.provider_type,
        payload.auth_type,
        payload.base_url,
    )
    creds = _sanitize_credentials_for_store(normalized_auth, payload.credentials)
    row = ProjectKnowledgeSource(
        project_id=project_id,
        name=payload.name.strip() or "外部知识库",
        provider_type=payload.provider_type.strip(),
        base_url=normalized_url,
        auth_type=normalized_auth,
        config_json=json.dumps(payload.config or {}, ensure_ascii=False),
        credentials_json=json.dumps(creds, ensure_ascii=False),
        weight=payload.weight,
        priority=payload.priority,
        enabled=payload.enabled,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _to_out(row)


@router.put(
    "/api/projects/{project_id}/knowledge-sources/{source_id}",
    response_model=KnowledgeSourceOut,
)
def update_knowledge_source(
    project_id: int,
    source_id: int,
    payload: KnowledgeSourceUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> KnowledgeSourceOut:
    require_request_project(request, project_id)
    _require_admin(request)
    _get_project(session, project_id)
    row = _get_source(session, project_id, source_id)

    next_provider_type = (
        payload.provider_type.strip()
        if payload.provider_type is not None
        else row.provider_type
    )
    next_auth_type = (
        payload.auth_type.strip().lower()
        if payload.auth_type is not None
        else row.auth_type
    )
    next_base_url = payload.base_url if payload.base_url is not None else row.base_url

    _, normalized_auth, normalized_url = _validate_provider_and_auth(
        next_provider_type,
        next_auth_type,
        next_base_url,
    )
    existing_creds_raw = row.credentials_json if normalized_auth == row.auth_type else None
    merged_creds = _sanitize_credentials_for_store(
        normalized_auth,
        payload.credentials,
        existing_creds_raw,
    )

    if payload.name is not None:
        row.name = payload.name.strip() or "外部知识库"
    row.provider_type = next_provider_type
    row.base_url = normalized_url
    row.auth_type = normalized_auth
    if payload.config is not None:
        row.config_json = json.dumps(payload.config, ensure_ascii=False)
    row.credentials_json = json.dumps(merged_creds, ensure_ascii=False)
    if payload.weight is not None:
        row.weight = payload.weight
    if payload.priority is not None:
        row.priority = payload.priority
    if payload.enabled is not None:
        row.enabled = payload.enabled
    row.updated_at = _utcnow()

    session.add(row)
    session.commit()
    session.refresh(row)
    return _to_out(row)


@router.delete(
    "/api/projects/{project_id}/knowledge-sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_knowledge_source(
    project_id: int,
    source_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> None:
    require_request_project(request, project_id)
    _require_admin(request)
    _get_project(session, project_id)
    row = _get_source(session, project_id, source_id)
    session.delete(row)
    session.commit()


@router.post(
    "/api/projects/{project_id}/knowledge-sources/test",
    response_model=KnowledgeSourceTestResponse,
)
def test_knowledge_source_connection(
    project_id: int,
    payload: KnowledgeSourceTestRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> KnowledgeSourceTestResponse:
    require_request_project(request, project_id)
    _require_admin(request)
    _get_project(session, project_id)

    existing_raw: str | None = None
    if payload.source_id is not None:
        existing = _get_source(session, project_id, payload.source_id)
        if existing.auth_type == payload.auth_type.strip().lower():
            existing_raw = existing.credentials_json

    provider, normalized_auth, normalized_url = _validate_provider_and_auth(
        payload.provider_type,
        payload.auth_type,
        payload.base_url,
    )
    merged_creds = _sanitize_credentials_for_store(
        normalized_auth,
        payload.credentials,
        existing_raw,
    )
    temp_source = ProjectKnowledgeSource(
        id=payload.source_id,
        project_id=project_id,
        name="连接测试",
        provider_type=payload.provider_type.strip(),
        base_url=normalized_url,
        auth_type=normalized_auth,
        config_json=json.dumps(payload.config or {}, ensure_ascii=False),
        credentials_json=json.dumps(merged_creds, ensure_ascii=False),
        enabled=True,
    )
    try:
        result = provider.test_connection(temp_source)
    except KnowledgeProviderError as exc:
        return KnowledgeSourceTestResponse(
            ok=False,
            message=exc.message,
            scopes=[],
        )
    return KnowledgeSourceTestResponse(
        ok=bool(result.get("ok")),
        message=str(result.get("message") or ""),
        scopes=list(result.get("scopes") or []),
    )


@router.get(
    "/api/projects/{project_id}/knowledge-sources/{source_id}/files",
    response_model=list[RemoteFileItem],
)
def proxy_list_knowledge_source_files(
    project_id: int,
    source_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> list[RemoteFileItem]:
    require_request_project(request, project_id)
    _get_project(session, project_id)
    try:
        return list_source_files(session, project_id, source_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KnowledgeProviderError as exc:
        raise _map_provider_error(exc) from exc


@router.get(
    "/api/projects/{project_id}/knowledge-sources/{source_id}/file-content",
    response_model=RemoteFileContent,
)
def proxy_read_knowledge_source_file(
    project_id: int,
    source_id: int,
    request: Request,
    path: str = Query(""),
    session: Session = Depends(get_session),
) -> RemoteFileContent:
    require_request_project(request, project_id)
    _get_project(session, project_id)
    try:
        return read_source_file(session, project_id, source_id, path)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except KnowledgeProviderError as exc:
        raise _map_provider_error(exc) from exc
