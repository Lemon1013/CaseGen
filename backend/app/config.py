import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("APP_DATA_DIR", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw" / "sources"
META_DIR = DATA_DIR / "meta"
DB_PATH = META_DIR / "app.db"
LLM_DEFAULT_TIMEOUT_SEC = int(os.getenv("LLM_DEFAULT_TIMEOUT_SEC", "180"))
RETRIEVE_TOP_K = int(os.getenv("RETRIEVE_TOP_K", "6"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "18000"))
FINAL_SCORE_THRESHOLD = int(os.getenv("FINAL_SCORE_THRESHOLD", "80"))
TASK_MAX_CONCURRENT = max(1, int(os.getenv("TASK_MAX_CONCURRENT", "4")))
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {".md", ".txt", ".pdf", ".docx"}

# Authentication is enabled by default in production.  The test suite may
# explicitly disable it through its fixture for legacy pipeline tests; no
# production code path defaults to that compatibility mode.
AUTH_ENABLED = os.getenv("CASEGEN_AUTH_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
AUTH_COOKIE_NAME = os.getenv("CASEGEN_AUTH_COOKIE_NAME", "casegen_session")
AUTH_CSRF_COOKIE_NAME = os.getenv("CASEGEN_AUTH_CSRF_COOKIE_NAME", "casegen_csrf")
AUTH_SESSION_DAYS = max(1, int(os.getenv("CASEGEN_AUTH_SESSION_DAYS", "7")))
AUTH_MAX_SESSIONS_PER_USER = max(1, int(os.getenv("CASEGEN_AUTH_MAX_SESSIONS", "8")))
# Public self-service registration defaults to enabled; administrators can
# still switch it off at runtime through /api/auth/registration-setting.
ALLOW_REGISTRATION = os.getenv("CASEGEN_ALLOW_REGISTRATION", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
_cookie_secure_raw = os.getenv("CASEGEN_AUTH_COOKIE_SECURE", "auto").strip().lower()
AUTH_COOKIE_SECURE: bool | None = (
    None
    if _cookie_secure_raw in {"", "auto", "inherit"}
    else _cookie_secure_raw in {"1", "true", "yes", "on"}
)
_cors_origins_raw = os.getenv(
    "CASEGEN_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
)
# Never pass ``*`` to credentialed CORS.  A wildcard in an operator-provided
# env var is ignored; same-origin requests remain allowed by the middleware.
CORS_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in _cors_origins_raw.split(",")
    if origin.strip() and origin.strip() != "*"
]

# ---------------------------------------------------------------------------
# DSH agent integration (docs/superpowers/specs/2026-11-04-dsh-agent-integration-design.md)
# ---------------------------------------------------------------------------
# Base URL of the local DSH web service plugin.  This is a loopback service
# address, not a model credential, so it is safe to keep in .env.
DSH_BASE_URL = os.getenv("DSH_BASE_URL", "http://127.0.0.1:31800/api/v1").rstrip("/")
# Optional bearer token for a future DSH with authEnabled=true.  Empty means
# no Authorization header is sent (current DSH builds bind 127.0.0.1 only).
DSH_API_TOKEN = os.getenv("DSH_API_TOKEN", "").strip()
AGENT_ENABLED = (
    os.getenv("AGENT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
)
# Maximum number of concurrent agent turns in this single process.
AGENT_MAX_CONCURRENT = max(1, int(os.getenv("AGENT_MAX_CONCURRENT", "4")))
# Default keeps workspaces out of the repo: ``~`` is expanded by
# ``agent_workspace_root()`` (Path.expanduser) on Linux and Windows.  Set an
# empty value to fall back to ``DATA_DIR / "agent-workspace"`` (test fixtures
# redirect ``DATA_DIR`` to a temporary directory).  ``data/meta`` holds
# plaintext model API keys and is never inside this root.
AGENT_WORKSPACE_ROOT = os.getenv("AGENT_WORKSPACE_ROOT", "").strip()
# DSH can take 5-8 minutes to emit the first token while a model loads, so the
# read timeout must stay far above the LLM defaults.
AGENT_FIRST_TOKEN_TIMEOUT_SEC = max(30, int(os.getenv("AGENT_FIRST_TOKEN_TIMEOUT_SEC", "600")))
# Wall-clock budget for a stalled turn (no event at all) before it is failed.
AGENT_STALL_TIMEOUT_SEC = max(10, int(os.getenv("AGENT_STALL_TIMEOUT_SEC", "300")))
# Line-B budget gate: an agent-backed pipeline stage may not exceed this many
# DSH calls per task before it falls back to the legacy path.
AGENT_STAGE_MAX_CALLS = max(1, int(os.getenv("AGENT_STAGE_MAX_CALLS", "4")))
# Comma-separated pipeline stage names allowed to run through the agent.
# Empty keeps every stage on the legacy deterministic path.
PIPELINE_AGENT_STAGES = os.getenv("PIPELINE_AGENT_STAGES", "").strip()
# Repository-relative directory holding expert skills (``*/SKILL.md``).
AGENT_SKILLS_DIR = os.getenv("AGENT_SKILLS_DIR", ".agents/skills").strip()
# DSH agent preset id used when CaseGen creates a DSH session. Empty keeps the
# deployment default; set it to the installed CaseGen preset id to force it.
AGENT_PRESET = os.getenv("AGENT_PRESET", "").strip()


def agent_workspace_root() -> Path:
    """Return the resolved agent workspace root, re-read on every call."""

    raw = (AGENT_WORKSPACE_ROOT or "").strip()
    if not raw:
        return Path(DATA_DIR) / "agent-workspace"
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else ROOT / candidate


def agent_pipeline_stages() -> set[str]:
    """Parse ``PIPELINE_AGENT_STAGES`` into a set of stage names."""

    return {
        item.strip()
        for item in (PIPELINE_AGENT_STAGES or "").split(",")
        if item.strip()
    }


def ensure_data_dirs() -> None:
    for p in (RAW_DIR, META_DIR):
        p.mkdir(parents=True, exist_ok=True)
