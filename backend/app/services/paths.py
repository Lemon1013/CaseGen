from pathlib import Path
from uuid import uuid4

from app import config


def make_raw_filename(original_filename: str) -> str:
    """Build a unique on-disk name: `{uuid}_{filename}`."""
    safe_name = Path(original_filename).name
    return f"{uuid4().hex}_{safe_name}"


def raw_path_for(stored_name: str) -> Path:
    """Absolute path under RAW_DIR for a stored file name."""
    return config.RAW_DIR / stored_name


def relative_raw_stored_path(stored_name: str) -> str:
    """Relative path stored on Document.stored_path (posix style)."""
    return f"raw/sources/{stored_name}"
