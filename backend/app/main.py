from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session

from app.api.documents import router as documents_router
from app.api.models_cfg import router as models_router
from app.api.prompts import router as prompts_router
from app.config import ensure_data_dirs
from app.db import get_engine, init_db
from app.services.prompts_seed import seed_default_prompts


def create_app() -> FastAPI:
    ensure_data_dirs()
    init_db()
    with Session(get_engine()) as session:
        seed_default_prompts(session)

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

    return app


app = create_app()
