import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


@pytest.fixture
def tmp_app_data(tmp_path, monkeypatch):
    """Point app data paths at a temporary directory and reset the DB engine."""
    from app import config
    from app.db import reset_engine

    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw" / "sources"
    wiki_dir = data_dir / "wiki"
    wiki_pages_dir = wiki_dir / "pages"
    meta_dir = data_dir / "meta"
    db_path = meta_dir / "app.db"

    monkeypatch.setenv("APP_DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "RAW_DIR", raw_dir)
    monkeypatch.setattr(config, "WIKI_DIR", wiki_dir)
    monkeypatch.setattr(config, "WIKI_PAGES_DIR", wiki_pages_dir)
    monkeypatch.setattr(config, "META_DIR", meta_dir)
    monkeypatch.setattr(config, "DB_PATH", db_path)

    reset_engine()
    yield data_dir
    reset_engine()
