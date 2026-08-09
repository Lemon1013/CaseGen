import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("APP_DATA_DIR", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw" / "sources"
WIKI_DIR = DATA_DIR / "wiki"
WIKI_PAGES_DIR = WIKI_DIR / "pages"
META_DIR = DATA_DIR / "meta"
DB_PATH = META_DIR / "app.db"
BUNDLED_WIKI_DIR = Path(__file__).resolve().parent / "default_wiki"
LLM_DEFAULT_TIMEOUT_SEC = int(os.getenv("LLM_DEFAULT_TIMEOUT_SEC", "180"))
LLM_WIKI_TIMEOUT_SEC = int(os.getenv("LLM_WIKI_TIMEOUT_SEC", "300"))
LLM_WIKI_STREAM = os.getenv("LLM_WIKI_STREAM", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
LLM_WIKI_MAX_TOKENS = int(os.getenv("LLM_WIKI_MAX_TOKENS", "16384"))
LLM_WIKI_THINKING = os.getenv("LLM_WIKI_THINKING", "auto").strip().lower()
RETRIEVE_TOP_K = int(os.getenv("RETRIEVE_TOP_K", "6"))
# Hybrid retrieve: structured wiki pages + verbatim source chunks
RETRIEVE_WIKI_TOP_K = int(os.getenv("RETRIEVE_WIKI_TOP_K", "4"))
RETRIEVE_SOURCE_TOP_K = int(os.getenv("RETRIEVE_SOURCE_TOP_K", "4"))
MAX_WIKI_CONTEXT_CHARS = int(os.getenv("MAX_WIKI_CONTEXT_CHARS", "12000"))
MAX_SOURCE_CONTEXT_CHARS = int(os.getenv("MAX_SOURCE_CONTEXT_CHARS", "6000"))
SOURCE_CHUNK_CHARS = int(os.getenv("SOURCE_CHUNK_CHARS", "1200"))
SOURCE_CHUNK_OVERLAP = int(os.getenv("SOURCE_CHUNK_OVERLAP", "150"))
# Wiki long-source analyze (full-doc coverage; see wiki_long_analyze.py)
WIKI_ANALYZE_SINGLE_PASS_CHARS = int(os.getenv("WIKI_ANALYZE_SINGLE_PASS_CHARS", "6000"))
WIKI_ANALYZE_WINDOW_CHARS = int(os.getenv("WIKI_ANALYZE_WINDOW_CHARS", "6000"))
WIKI_ANALYZE_WINDOW_OVERLAP = int(os.getenv("WIKI_ANALYZE_WINDOW_OVERLAP", "600"))
WIKI_ANALYZE_DIGEST_MAX = int(os.getenv("WIKI_ANALYZE_DIGEST_MAX", "12000"))
WIKI_ANALYZE_PARTIAL_JSON_MAX = int(os.getenv("WIKI_ANALYZE_PARTIAL_JSON_MAX", "8000"))
WIKI_WRITE_ANALYSIS_CHARS = int(os.getenv("WIKI_WRITE_ANALYSIS_CHARS", "24000"))
WIKI_ANALYZE_WINDOW_RETRIES = int(os.getenv("WIKI_ANALYZE_WINDOW_RETRIES", "2"))
WIKI_ANALYZE_REPAIR_RETRIES = int(os.getenv("WIKI_ANALYZE_REPAIR_RETRIES", "1"))
WIKI_ANALYZE_REPAIR_CONTEXT_CHARS = int(os.getenv("WIKI_ANALYZE_REPAIR_CONTEXT_CHARS", "16000"))
WIKI_ANALYZE_MAX_OPERATIONS = int(os.getenv("WIKI_ANALYZE_MAX_OPERATIONS", "7"))
FINAL_SCORE_THRESHOLD = int(os.getenv("FINAL_SCORE_THRESHOLD", "80"))
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {".md", ".txt", ".pdf", ".docx"}


def ensure_data_dirs() -> None:
    for p in (RAW_DIR, WIKI_PAGES_DIR, META_DIR):
        p.mkdir(parents=True, exist_ok=True)

    for filename in ("purpose.md", "schema.md"):
        runtime_file = WIKI_DIR / filename
        if not runtime_file.exists():
            bundled_file = BUNDLED_WIKI_DIR / filename
            runtime_file.write_text(
                bundled_file.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

    index = WIKI_DIR / "index.md"
    if not index.exists():
        index.write_text("# Wiki Index\n\n", encoding="utf-8")
