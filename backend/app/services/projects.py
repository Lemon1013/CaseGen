from fastapi import HTTPException, Request
from sqlmodel import Session, select

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
    ProjectWikiBinding,
    Requirement,
    SourceChunk,
    TestCase,
    WikiPageRow,
    WikiReviewItem,
    WikiSpace,
)


def project_space_ids(session: Session, project_id: int, *, include_shared: bool = True) -> list[int]:
    project = session.get(Project, project_id)
    if project is None or project.status != "active":
        raise ValueError("Project not found or inactive")
    ids = [
        int(row.id)
        for row in session.exec(select(WikiSpace).where(
            WikiSpace.project_id == project_id,
            WikiSpace.scope == "project",
            WikiSpace.status == "active",
        )).all()
        if row.id is not None
    ]
    if include_shared:
        bindings = session.exec(select(ProjectWikiBinding).where(
            ProjectWikiBinding.project_id == project_id,
            ProjectWikiBinding.enabled == True,  # noqa: E712
        )).all()
        for binding in bindings:
            space = session.get(WikiSpace, binding.wiki_space_id)
            if space is not None and space.scope == "shared" and space.status == "active":
                ids.append(int(space.id))
    return list(dict.fromkeys(ids))


def validate_project_space(session: Session, project_id: int, space_id: int) -> WikiSpace:
    if int(space_id) not in project_space_ids(session, project_id):
        raise ValueError("Wiki space is not available to this project")
    row = session.get(WikiSpace, space_id)
    if row is None:
        raise ValueError("Wiki space not found")
    return row


def resource_space_id(session: Session, path: str) -> int | None:
    """Resolve the owning space for resource URLs checked by project middleware."""

    import re

    mappings = (
        (r"/api/wiki-spaces/(\d+)(?:/.*)?", WikiSpace, "id"),
        (r"/api/documents/(\d+)(?:/.*)?", Document, "space_id"),
        (r"/api/ingest-jobs/(\d+)(?:/.*)?", IngestJob, "space_id"),
        (r"/api/wiki/pages/(\d+)(?:/.*)?", WikiPageRow, "space_id"),
        (r"/api/wiki/reviews/(\d+)(?:/.*)?", WikiReviewItem, "space_id"),
        (r"/api/source-chunks/(\d+)(?:/.*)?", SourceChunk, "space_id"),
        (r"/api/data-pools/(\d+)(?:/.*)?", DataPool, "wiki_space_id"),
        (r"/api/platforms/(\d+)(?:/.*)?", PlatformProfile, "wiki_space_id"),
        (r"/api/platform-cases/(\d+)(?:/.*)?", ManagedPlatformCase, "wiki_space_id"),
        (r"/api/platform-renders/(\d+)(?:/.*)?", PlatformRenderRun, "wiki_space_id"),
    )
    for pattern, model, field in mappings:
        match = re.fullmatch(pattern, path)
        if not match:
            continue
        row = session.get(model, int(match.group(1)))
        value = getattr(row, field, None) if row is not None else None
        if value is None and isinstance(row, WikiReviewItem):
            page = session.get(WikiPageRow, row.page_id) if row.page_id else None
            value = page.space_id if page is not None else None
        if value is None and isinstance(row, IngestJob):
            document = session.get(Document, row.document_id)
            value = document.space_id if document is not None else None
        return int(value) if value is not None else None
    return None


def validate_request_project_scope(
    session: Session,
    project_id: int,
    path: str,
    query_space_ids: list[int],
) -> None:
    """Validate every space named directly or implied by a resource URL."""

    for space_id in dict.fromkeys(query_space_ids):
        validate_project_space(session, project_id, space_id)
    resource_id = resource_space_id(session, path)
    if resource_id is not None:
        validate_project_space(session, project_id, resource_id)

    import re

    task_match = re.fullmatch(r"/api/tasks/(\d+)(?:/.*)?", path)
    if task_match:
        task = session.get(GenerationTask, int(task_match.group(1)))
        if task is None or task.project_id != project_id:
            raise ValueError("Task not found in project")
    case_match = re.fullmatch(r"/api/cases/(\d+)(?:/.*)?", path)
    if case_match:
        case = session.get(TestCase, int(case_match.group(1)))
        requirement = session.get(Requirement, case.requirement_id) if case else None
        if case is None or requirement is None or requirement.project_id != project_id:
            raise ValueError("Test case not found in project")


_PROJECT_SCOPED_PREFIXES = (
    "/api/documents",
    "/api/wiki-spaces",
    "/api/ingest-jobs",
    "/api/wiki/pages",
    "/api/wiki/reviews",
    "/api/wiki/retrieve",
    "/api/source-chunks",
    "/api/requirements",
    "/api/tasks",
    "/api/cases",
    "/api/data-pools",
    "/api/platforms",
    "/api/platform-cases",
    "/api/platform-semantic-cases",
    "/api/platform-renders",
)


def is_project_scoped_path(path: str) -> bool:
    if path.startswith("/api/projects/"):
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _PROJECT_SCOPED_PREFIXES)


def require_request_project(request: Request, target_project_id: int) -> int:
    """Require the authenticated request context to match a body/path target."""

    raw = request.query_params.get("project_id")
    if raw is None and not config.AUTH_ENABLED:
        return int(target_project_id)
    if raw is None or not raw.isdigit():
        raise HTTPException(status_code=422, detail="project_id is required")
    if int(raw) != int(target_project_id):
        raise HTTPException(status_code=404, detail="Resource not found in project")
    return int(raw)


def request_project_id(request: Request) -> int | None:
    raw = request.query_params.get("project_id")
    if raw is None:
        if config.AUTH_ENABLED:
            raise HTTPException(status_code=422, detail="project_id is required")
        return None
    if not raw.isdigit():
        raise HTTPException(status_code=422, detail="project_id must be a positive integer")
    return int(raw)
