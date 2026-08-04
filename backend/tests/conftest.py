import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Some test modules import app.main during collection, which initializes the
# database before per-test fixtures run.  Point both application data and
# Python's temp directory at an ignored workspace path before collection so a
# test run can never migrate or seed the developer's real data/ directory.
_PYTEST_SESSION_ROOT = BACKEND_ROOT / ".pytest_cache" / f"casegen-{os.getpid()}"


def pytest_sessionstart(session):
    data_dir = _PYTEST_SESSION_ROOT / "data"
    temp_dir = _PYTEST_SESSION_ROOT / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    os.environ["APP_DATA_DIR"] = str(data_dir)
    for name in ("TEMP", "TMP", "TMPDIR"):
        os.environ[name] = str(temp_dir)
    tempfile.tempdir = str(temp_dir)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_PYTEST_SESSION_ROOT, ignore_errors=True)


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
