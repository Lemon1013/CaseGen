from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import ensure_data_dirs


def create_app() -> FastAPI:
    ensure_data_dirs()
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

    return app


app = create_app()
