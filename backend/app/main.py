from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from starlette.responses import FileResponse

from app.api.documents import router as documents_router
from app.api.models_cfg import router as models_router
from app.api.prompts import router as prompts_router
from app.api.requirements import router as requirements_router
from app.api.tasks import router as tasks_router
from app.api.wiki import router as wiki_router
from app.api.wiki_spaces import router as wiki_spaces_router
from app.config import ensure_data_dirs
from app.db import get_engine, init_db
from app.services.prompts_seed import seed_default_prompts
from app.services.wiki_jobs import recover_ingest_jobs


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

    app = FastAPI(title="CaseGen API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    app.include_router(documents_router)
    app.include_router(models_router)
    app.include_router(prompts_router)
    app.include_router(requirements_router)
    app.include_router(tasks_router)
    app.include_router(wiki_router)
    app.include_router(wiki_spaces_router)

    _mount_frontend_dist(app)

    return app


app = create_app()
