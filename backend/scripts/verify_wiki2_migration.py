"""Copy a CaseGen data directory and verify Wiki 2.0 migration safely.

The source directory is never opened for writing. The copied database is
migrated, checked with SQLite integrity_check, and its FTS projection rebuilt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-data", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    source = args.source_data.resolve()
    output = args.output.resolve()
    if not source.is_dir():
        parser.error(f"source data directory does not exist: {source}")
    if output.exists():
        parser.error(f"output already exists; refusing to overwrite: {output}")
    try:
        output.relative_to(source)
    except ValueError:
        pass
    else:
        parser.error("output must not be inside source data directory")

    source_db = source / "meta" / "app.db"
    source_hash_before = _sha256(source_db)
    started = time.perf_counter()
    shutil.copytree(source, output)
    copy_seconds = time.perf_counter() - started

    os.environ["APP_DATA_DIR"] = str(output)
    backend_root = Path(__file__).resolve().parents[1]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    from sqlmodel import Session, select

    from app.db import get_engine, init_db
    from app.models.entities import SourceChunk, WikiPageRow
    from app.services.wiki_fts import index_counts, rebuild_fts

    migration_started = time.perf_counter()
    init_db()
    engine = get_engine()
    with Session(engine) as session:
        pages = list(session.exec(select(WikiPageRow)).all())
        chunks = list(session.exec(select(SourceChunk)).all())
        fts = dict(rebuild_fts(session, pages, chunks))
        session.commit()
        counts = index_counts(session)
    migration_seconds = time.perf_counter() - migration_started

    copied_db = output / "meta" / "app.db"
    with sqlite3.connect(copied_db) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())

    source_hash_after = _sha256(source_db)
    report = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "source_data": str(source),
        "output_data": str(output),
        "source_db_sha256_before": source_hash_before,
        "source_db_sha256_after": source_hash_after,
        "source_unchanged": source_hash_before == source_hash_after,
        "copied_db_sha256_after": _sha256(copied_db),
        "copy_seconds": round(copy_seconds, 4),
        "migration_and_rebuild_seconds": round(migration_seconds, 4),
        "sqlite_integrity_check": integrity,
        "foreign_key_violations": foreign_key_violations,
        "wiki_pages": counts.get("wiki_pages", 0),
        "source_chunks": counts.get("source_chunks", 0),
        "fts": fts,
    }
    report_path = output / "wiki2-migration-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if integrity == "ok" and report["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
