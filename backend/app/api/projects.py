from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import get_session
from app.models.entities import (
    DataPool,
    GenerationTask,
    ManagedPlatformCase,
    PlatformProfile,
    PlatformRenderRun,
    Project,
    Requirement,
    WikiSpace,
)
from app.models.external_wiki import ProjectKnowledgeSource
from app.schemas.projects import ProjectCreate, ProjectOut, ProjectSpaceOut, ProjectUpdate
from app.services.knowledge_providers.service import _is_source_configured
from app.services.permissions import current_user, is_admin, require_admin
from app.services.wiki_spaces import DEFAULT_SPACE_SLUG, normalize_space_slug, slug_from_name

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _require_admin(request: Request) -> None:
    require_admin(request, detail="Project management requires an administrator")


def _require_project(request: Request, project_id: int) -> None:
    from app.services.projects import require_request_project
    require_request_project(request, project_id)


def _out(session: Session, row: Project) -> ProjectOut:
    private = session.exec(select(WikiSpace).where(WikiSpace.project_id == row.id, WikiSpace.scope == "project")).all()
    sources = session.exec(
        select(ProjectKnowledgeSource).where(
            ProjectKnowledgeSource.project_id == row.id,
            ProjectKnowledgeSource.enabled == True,  # noqa: E712
        )
    ).all()
    knowledge_source_count = sum(1 for src in sources if _is_source_configured(src))
    return ProjectOut(
        **row.model_dump(),
        knowledge_source_count=knowledge_source_count,
        private_spaces=[ProjectSpaceOut(**space.model_dump(), enabled=True) for space in private],
        shared_spaces=[],
    )


def _get(session: Session, project_id: int) -> Project:
    row = session.get(Project, project_id)
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def _project_has_data(session: Session, project_id: int, space_ids: list[int]) -> bool:
    direct = (
        (Requirement, Requirement.project_id),
        (GenerationTask, GenerationTask.project_id),
        (ProjectKnowledgeSource, ProjectKnowledgeSource.project_id),
    )
    if any(session.exec(select(model.id).where(column == project_id)).first() is not None for model, column in direct):
        return True
    scoped = (
        (DataPool, DataPool.wiki_space_id),
        (PlatformProfile, PlatformProfile.wiki_space_id),
        (PlatformRenderRun, PlatformRenderRun.wiki_space_id),
        (ManagedPlatformCase, ManagedPlatformCase.wiki_space_id),
    )
    return bool(space_ids) and any(
        session.exec(select(model.id).where(column.in_(space_ids))).first() is not None
        for model, column in scoped
    )


@router.get("", response_model=list[ProjectOut])
def list_projects(request: Request, session: Session = Depends(get_session)):
    # Ordinary users only see projects an administrator marked as visible;
    # administrators need the full list to configure the switch.  When auth
    # is disabled (legacy test compatibility) everyone sees everything.
    query = select(Project)
    if not is_admin(request) and current_user(request) is not None:
        query = query.where(Project.is_public == True)  # noqa: E712
    rows = session.exec(query.order_by(Project.status, Project.name)).all()
    return [_out(session, row) for row in rows]


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectCreate, request: Request, session: Session = Depends(get_session)):
    _require_admin(request)
    try:
        slug = normalize_space_slug(body.slug or slug_from_name(body.name))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    row = Project(name=body.name.strip(), slug=slug, description=body.description.strip())
    session.add(row)
    try:
        session.flush()
        space = WikiSpace(name=f"{row.name} 知识库", slug=f"{slug}-wiki", scope="project", project_id=row.id)
        session.add(space)
        session.flush()
        row.default_wiki_space_id = space.id
        session.add(row)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "Project or Wiki space slug already exists") from exc
    session.refresh(row)
    return _out(session, row)


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, request: Request, session: Session = Depends(get_session)):
    _require_project(request, project_id)
    return _out(session, _get(session, project_id))


@router.put("/{project_id}", response_model=ProjectOut)
def update_project(project_id: int, body: ProjectUpdate, request: Request, session: Session = Depends(get_session)):
    _require_project(request, project_id)
    _require_admin(request)
    row = _get(session, project_id)
    next_status = body.status.strip() if body.status is not None else row.status
    if next_status == "archived" and row.status != "archived":
        if row.slug == DEFAULT_SPACE_SLUG:
            raise HTTPException(409, "The default project cannot be archived")
        active_count = len(session.exec(select(Project.id).where(Project.status == "active")).all())
        if active_count <= 1:
            raise HTTPException(409, "At least one active project is required")
    for name in ("name", "description", "status"):
        value = getattr(body, name)
        if value is not None:
            setattr(row, name, value.strip())
    if body.is_public is not None:
        row.is_public = bool(body.is_public)
    if row.status not in {"active", "archived"}:
        raise HTTPException(422, "Unsupported project status")
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(row); session.commit(); session.refresh(row)
    return _out(session, row)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: int, request: Request, session: Session = Depends(get_session)) -> None:
    _require_project(request, project_id)
    _require_admin(request)
    row = _get(session, project_id)
    if row.slug == DEFAULT_SPACE_SLUG:
        raise HTTPException(409, "The default project cannot be deleted")
    if len(session.exec(select(Project.id)).all()) <= 1:
        raise HTTPException(409, "The last project cannot be deleted")
    if row.status == "active":
        remaining_active = session.exec(select(Project.id).where(Project.status == "active", Project.id != project_id)).first()
        if remaining_active is None:
            raise HTTPException(409, "At least one active project is required")
    spaces = session.exec(select(WikiSpace).where(WikiSpace.project_id == project_id, WikiSpace.scope == "project")).all()
    space_ids = [int(space.id) for space in spaces if space.id is not None]
    if _project_has_data(session, project_id, space_ids):
        raise HTTPException(409, "Project contains business data")
    try:
        row.default_wiki_space_id = None
        session.add(row)
        session.flush()
        for space in spaces:
            session.delete(space)
        session.delete(row)
        session.commit()
    except Exception as exc:
        session.rollback()
        raise HTTPException(500, f"Failed to delete project safely: {exc}") from exc
