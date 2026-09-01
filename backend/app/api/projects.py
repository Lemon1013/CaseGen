from datetime import datetime, timezone
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db import get_session
from app import config
from app.models.entities import (
    DataPool,
    Document,
    GenerationTask,
    IngestJob,
    ManagedPlatformCase,
    PlatformProfile,
    PlatformRenderRun,
    Project,
    ProjectKnowledgeDecision,
    ProjectWikiBinding,
    Requirement,
    SourceChunk,
    WikiPageRow,
    WikiReviewItem,
    WikiSpace,
)
from app.schemas.projects import ProjectBindingIn, ProjectCreate, ProjectOut, ProjectSpaceOut, ProjectUpdate
from app.services.wiki_spaces import normalize_space_slug, slug_from_name
from app.services.wiki_spaces import DEFAULT_SPACE_SLUG, ensure_space_dirs, space_root

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _require_admin(request: Request) -> None:
    if not config.AUTH_ENABLED:
        return
    user = getattr(request.state, "user", None)
    if user is None or getattr(user, "role", "") != "admin":
        raise HTTPException(status_code=403, detail="Project management requires an administrator")


def _require_project(request: Request, project_id: int) -> None:
    from app.services.projects import require_request_project
    require_request_project(request, project_id)


def _out(session: Session, row: Project) -> ProjectOut:
    private = session.exec(select(WikiSpace).where(WikiSpace.project_id == row.id, WikiSpace.scope == "project")).all()
    bindings = session.exec(select(ProjectWikiBinding).where(ProjectWikiBinding.project_id == row.id).order_by(ProjectWikiBinding.priority)).all()
    shared_by_id = {space.id: space for space in session.exec(select(WikiSpace).where(WikiSpace.scope == "shared")).all()}
    return ProjectOut(
        **row.model_dump(),
        private_spaces=[ProjectSpaceOut(**space.model_dump(), enabled=True) for space in private],
        shared_spaces=[ProjectSpaceOut(**shared_by_id[b.wiki_space_id].model_dump(), priority=b.priority, enabled=b.enabled) for b in bindings if b.wiki_space_id in shared_by_id],
    )


def _get(session: Session, project_id: int) -> Project:
    row = session.get(Project, project_id)
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def _ensure_deletable_space_root(space: WikiSpace):
    root = space_root(space)
    if not root.exists():
        return root
    allowed_dirs = {"pages", "sources", "rules", "entities", "scenarios", "regressions", "synthesis"}
    allowed_files = {
        "index.md": "# Wiki Index\n\n",
        "overview.md": "# Wiki Overview\n\n",
        "log.md": "# Wiki Log\n\n",
    }
    for path in root.rglob("*"):
        if path.is_symlink():
            raise HTTPException(409, "Project Wiki directory contains a symbolic link")
        relative = path.relative_to(root)
        if path.is_dir():
            if len(relative.parts) != 1 or relative.name not in allowed_dirs:
                raise HTTPException(409, "Project Wiki directory contains non-standard content")
        elif len(relative.parts) != 1 or relative.name not in allowed_files:
            raise HTTPException(409, "Project Wiki directory contains non-standard content")
        elif path.read_text(encoding="utf-8") != allowed_files[relative.name]:
            raise HTTPException(409, "Project Wiki skeleton contains user content")
    return root


def _project_has_data(session: Session, project_id: int, space_ids: list[int]) -> bool:
    direct = (
        (Requirement, Requirement.project_id),
        (GenerationTask, GenerationTask.project_id),
        (ProjectKnowledgeDecision, ProjectKnowledgeDecision.project_id),
    )
    if any(session.exec(select(model.id).where(column == project_id)).first() is not None for model, column in direct):
        return True
    scoped = (
        (Document, Document.space_id),
        (IngestJob, IngestJob.space_id),
        (WikiPageRow, WikiPageRow.space_id),
        (WikiReviewItem, WikiReviewItem.space_id),
        (SourceChunk, SourceChunk.space_id),
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
def list_projects(session: Session = Depends(get_session)):
    return [_out(session, row) for row in session.exec(select(Project).order_by(Project.status, Project.name)).all()]


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
    ensure_space_dirs(space)
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
    roots = [_ensure_deletable_space_root(space) for space in spaces]
    bindings = session.exec(select(ProjectWikiBinding).where(ProjectWikiBinding.project_id == project_id)).all()
    removed_spaces: list[WikiSpace] = []
    try:
        for binding in bindings:
            session.delete(binding)
        row.default_wiki_space_id = None
        session.add(row)
        session.flush()
        for space in spaces:
            session.delete(space)
        session.delete(row)
        session.flush()
        for space, root in zip(spaces, roots):
            if root.exists():
                removed_spaces.append(space)
                shutil.rmtree(root)
        session.commit()
    except Exception as exc:
        session.rollback()
        recovery_errors: list[str] = []
        for space in removed_spaces:
            try:
                ensure_space_dirs(space)
            except Exception as recovery_exc:  # noqa: BLE001
                recovery_errors.append(str(recovery_exc))
        detail = f"Failed to delete project safely: {exc}"
        if recovery_errors:
            detail += f"; Wiki skeleton recovery failed: {'; '.join(recovery_errors)}"
        raise HTTPException(500, detail) from exc


@router.put("/{project_id}/shared-wikis/{space_id}", response_model=ProjectOut)
def bind_shared_wiki(project_id: int, space_id: int, body: ProjectBindingIn, request: Request, session: Session = Depends(get_session)):
    _require_project(request, project_id)
    _require_admin(request)
    project = _get(session, project_id)
    if body.wiki_space_id != space_id:
        raise HTTPException(422, "wiki_space_id does not match path")
    space = session.get(WikiSpace, space_id)
    if space is None or space.scope != "shared" or space.status != "active":
        raise HTTPException(422, "Only active shared Wiki spaces can be bound")
    binding = session.exec(select(ProjectWikiBinding).where(ProjectWikiBinding.project_id == project_id, ProjectWikiBinding.wiki_space_id == space_id)).first()
    if binding is None:
        binding = ProjectWikiBinding(project_id=project_id, wiki_space_id=space_id)
    binding.priority, binding.enabled = body.priority, body.enabled
    session.add(binding); session.commit()
    return _out(session, project)


@router.delete("/{project_id}/shared-wikis/{space_id}", response_model=ProjectOut)
def unbind_shared_wiki(project_id: int, space_id: int, request: Request, session: Session = Depends(get_session)):
    _require_project(request, project_id)
    _require_admin(request)
    project = _get(session, project_id)
    binding = session.exec(select(ProjectWikiBinding).where(ProjectWikiBinding.project_id == project_id, ProjectWikiBinding.wiki_space_id == space_id)).first()
    if binding is not None:
        session.delete(binding); session.commit()
    return _out(session, project)
