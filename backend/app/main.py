from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from sqlmodel import Session
from starlette.responses import FileResponse

from app.api.documents import router as documents_router
from app.api.auth import router as auth_router
from app.api.cases import router as cases_router
from app.api.models_cfg import router as models_router
from app.api.prompts import router as prompts_router
from app.api.requirements import router as requirements_router
from app.api.tasks import router as tasks_router
from app.api.wiki import router as wiki_router
from app.api.wiki_spaces import router as wiki_spaces_router
from app import config
from app.config import ensure_data_dirs
from app.db import get_engine, init_db
from app.services.auth import get_user_for_token
from app.services.prompts_seed import seed_default_prompts
from app.services.wiki_jobs import recover_ingest_jobs
from app.services.task_jobs import recover_generation_jobs


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
    # The durable Job table is the queue.  Recovery is idempotent and the
    # scheduler itself has one worker, so repeated app construction does not
    # create a second concurrent Wiki ingest worker.
    recover_ingest_jobs()
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

    @app.middleware("http")
    async def authentication_middleware(request: Request, call_next):
        # OPTIONS is handled by the CORS middleware and carries no session
        # mutation.  Legacy tests can explicitly disable auth through their
        # fixture; production defaults remain protected.
        if not config.AUTH_ENABLED or request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        if path.startswith("/api"):
            unsafe = request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}
            if unsafe:
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

            if path not in public_paths:
                with Session(get_engine()) as auth_session:
                    user = get_user_for_token(
                        auth_session,
                        request.cookies.get(config.AUTH_COOKIE_NAME),
                    )
                if user is None:
                    return JSONResponse({"detail": "Not authenticated"}, status_code=401)
                request.state.user = user

            # If a browser client sends a double-submit token, validate it.
            # Origin/Referer remains the mandatory CSRF boundary; optional
            # token validation also protects clients that opt into the header.
            csrf_header = request.headers.get("x-csrf-token")
            csrf_cookie = request.cookies.get(config.AUTH_CSRF_COOKIE_NAME)
            if unsafe and csrf_header is not None and csrf_header != csrf_cookie:
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)

        return await call_next(request)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    app.include_router(documents_router)
    app.include_router(auth_router)
    app.include_router(cases_router)
    app.include_router(models_router)
    app.include_router(prompts_router)
    app.include_router(requirements_router)
    app.include_router(tasks_router)
    app.include_router(wiki_router)
    app.include_router(wiki_spaces_router)

    _mount_frontend_dist(app)

    return app


app = create_app()
