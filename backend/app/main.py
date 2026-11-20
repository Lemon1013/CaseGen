from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from sqlmodel import Session
from starlette.responses import FileResponse

from app.api.auth import router as auth_router
from app.api.cases import router as cases_router
from app.api.models_cfg import router as models_router
from app.api.prompts import router as prompts_router
from app.api.requirements import router as requirements_router
from app.api.tasks import router as tasks_router
from app.api.wiki_spaces import router as wiki_spaces_router
from app.api.platform_data import router as platform_data_router
from app.api.projects import router as projects_router
from app.api.external_wikis import router as external_wikis_router
from app.api.knowledge_sources import router as knowledge_sources_router
from app.api.agent_chat import router as agent_chat_router
from app.api.users import router as users_router
from app.api.dashboard import router as dashboard_router
from app import config
from app.config import ensure_data_dirs
from app.db import get_engine, init_db
from app.services.auth import get_user_for_token
from app.services.permissions import current_user, is_admin, snapshot_user
from app.services.prompts_seed import seed_default_prompts
from app.services.task_jobs import recover_generation_jobs
from app.services.projects import (
    is_project_scoped_path,
    is_project_visibility_path,
    validate_request_project_scope,
)


def _mount_frontend_dist(app: FastAPI) -> None:
    """Serve the built frontend from frontend/dist in single-process mode.

    Enabled only when a production build exists, so dev machines keep the
    Vite proxy as the only frontend entry.  Real files (assets/, favicon)
    are served as-is; unknown paths fall back to index.html for SPA routes.
    The API routers are registered before this catch-all, so /api always
    wins over the static fallback.
    """
    dist_dir = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if not dist_dir.is_dir():
        return
    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str) -> FileResponse:
        target = dist_dir / full_path
        if target.is_file():
            return FileResponse(target)
        return FileResponse(dist_dir / "index.html")


def create_app() -> FastAPI:
    ensure_data_dirs()
    init_db()
    with Session(get_engine()) as session:
        seed_default_prompts(session)
    recover_generation_jobs()

    app = FastAPI(title="CaseGen API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        # Credentialed cookies must never be paired with wildcard origins.
        allow_origins=list(config.CORS_ORIGINS),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    public_paths = {
        "/api/health",
        "/api/auth/bootstrap",
        "/api/auth/setup",
        "/api/auth/register",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/session",
        "/api/auth/me",
    }

    def _same_origin(value: str, request: Request) -> bool:
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return False
            origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        except ValueError:
            return False
        host_origin = f"{request.url.scheme}://{request.headers.get('host', '')}".rstrip("/")
        allowed = {str(item).rstrip("/") for item in config.CORS_ORIGINS}
        allowed.add(host_origin)
        return origin in allowed

    def _is_positive_int_str(value: str | None) -> bool:
        return bool(value and value.isascii() and value.isdigit() and int(value) > 0)

    @app.middleware("http")
    async def authentication_middleware(request: Request, call_next):
        # OPTIONS is handled by the CORS middleware and carries no session
        # mutation.  Legacy tests can explicitly disable auth through their
        # fixture; production defaults remain protected.
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        if path.startswith("/api"):
            unsafe = request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}
            # Stateless double-submit CSRF check: when a client opts into
            # X-CSRF-Token, reject mismatches before opening any DB session.
            csrf_header = request.headers.get("x-csrf-token")
            csrf_cookie = request.cookies.get(config.AUTH_CSRF_COOKIE_NAME)
            if (
                config.AUTH_ENABLED
                and unsafe
                and csrf_header is not None
                and csrf_header != csrf_cookie
            ):
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)

            project_raw = request.query_params.get("project_id")
            need_auth = config.AUTH_ENABLED and path not in public_paths
            has_valid_project_raw = _is_positive_int_str(project_raw)
            need_visibility = (
                has_valid_project_raw
                and config.AUTH_ENABLED
                and is_project_visibility_path(path)
            )
            need_scope = (
                has_valid_project_raw
                and is_project_scoped_path(path)
                and not path.startswith("/api/projects/")
            )

            def _check_origin_and_project_param() -> JSONResponse | None:
                if config.AUTH_ENABLED and unsafe:
                    origin = request.headers.get("origin")
                    referer = request.headers.get("referer")
                    if origin:
                        if not _same_origin(origin, request):
                            return JSONResponse(
                                {"detail": "Cross-origin request blocked"}, status_code=403
                            )
                    elif referer:
                        if not _same_origin(referer, request):
                            return JSONResponse(
                                {"detail": "Cross-origin request blocked"}, status_code=403
                            )
                    else:
                        return JSONResponse(
                            {"detail": "Origin or Referer header is required"}, status_code=403
                        )

                if config.AUTH_ENABLED and is_project_scoped_path(path) and project_raw is None:
                    return JSONResponse({"detail": "project_id is required"}, status_code=422)
                if project_raw is not None and not has_valid_project_raw:
                    return JSONResponse(
                        {"detail": "project_id must be a positive integer"}, status_code=422
                    )
                return None

            # Use a single short-lived DB Session for all middleware checks
            # (token lookup + snapshot, project visibility, project scope), and
            # close it strictly before awaiting call_next(request).
            if need_auth or need_visibility or need_scope:
                with Session(get_engine()) as mw_session:
                    if need_auth:
                        user = get_user_for_token(
                            mw_session,
                            request.cookies.get(config.AUTH_COOKIE_NAME),
                        )
                        # Freeze the identity WHILE the session is still open.
                        # The ORM User becomes detached (and its attributes expire)
                        # as soon as this `with` block closes; keeping it on the
                        # request would make any later getattr(user, ...) attempt
                        # a lazy refresh and raise DetachedInstanceError -> 500.
                        identity = snapshot_user(user) if user is not None else None
                        if identity is None:
                            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
                        request.state.user = identity

                    precheck_error = _check_origin_and_project_param()
                    if precheck_error is not None:
                        return precheck_error

                    # Visibility fallback: a project hidden from ordinary users must
                    # be unreachable on project DATA paths such as /api/tasks and
                    # /api/cases, even though the scope check below deliberately skips
                    # the /api/projects/ prefix.  The 404 wording matches the existing
                    # scope guard so a hidden project is indistinguishable from one
                    # that does not exist.
                    if (
                        need_visibility
                        and current_user(request) is not None
                        and not is_admin(request)
                    ):
                        from app.models.entities import Project

                        project = mw_session.get(Project, int(project_raw))
                        if project is not None and not bool(project.is_public):
                            return JSONResponse(
                                {"detail": "Resource not found in project"}, status_code=404
                            )

                    if need_scope:
                        query_spaces = [
                            int(value)
                            for key in ("space_id", "wiki_space_id")
                            for value in request.query_params.getlist(key)
                            if _is_positive_int_str(value)
                        ]
                        try:
                            validate_request_project_scope(
                                mw_session,
                                int(project_raw),
                                request.url.path,
                                query_spaces,
                            )
                        except ValueError:
                            return JSONResponse(
                                {"detail": "Resource not found in project"}, status_code=404
                            )
            else:
                precheck_error = _check_origin_and_project_param()
                if precheck_error is not None:
                    return precheck_error

        return await call_next(request)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    app.include_router(auth_router)
    app.include_router(cases_router)
    app.include_router(models_router)
    app.include_router(prompts_router)
    app.include_router(requirements_router)
    app.include_router(tasks_router)
    app.include_router(wiki_spaces_router)
    app.include_router(platform_data_router)
    app.include_router(projects_router)
    app.include_router(external_wikis_router)
    app.include_router(knowledge_sources_router)
    app.include_router(users_router)
    app.include_router(dashboard_router)
    # Agent plane (line A).  Registered last so its /api/agent prefix can never
    # shadow an existing route, and before the SPA catch-all mount.
    app.include_router(agent_chat_router)

    _mount_frontend_dist(app)

    return app


app = create_app()
