import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("APP_DATA_DIR", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw" / "sources"
WIKI_DIR = DATA_DIR / "wiki"
WIKI_PAGES_DIR = WIKI_DIR / "pages"
META_DIR = DATA_DIR / "meta"
DB_PATH = META_DIR / "app.db"
LLM_DEFAULT_TIMEOUT_SEC = int(os.getenv("LLM_DEFAULT_TIMEOUT_SEC", "120"))
RETRIEVE_TOP_K = int(os.getenv("RETRIEVE_TOP_K", "6"))
MAX_WIKI_CONTEXT_CHARS = int(os.getenv("MAX_WIKI_CONTEXT_CHARS", "12000"))
FINAL_SCORE_THRESHOLD = int(os.getenv("FINAL_SCORE_THRESHOLD", "80"))
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {".md", ".txt", ".pdf"}


def ensure_data_dirs() -> None:
    for p in (RAW_DIR, WIKI_PAGES_DIR, META_DIR):
        p.mkdir(parents=True, exist_ok=True)
    index = WIKI_DIR / "index.md"
    if not index.exists():
        index.write_text("# Wiki Index\n\n", encoding="utf-8")
