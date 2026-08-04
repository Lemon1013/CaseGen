"""SQLite helpers for the Wiki 2.0 schema migration.

The application intentionally does not use Alembic.  This module keeps the
small amount of schema evolution needed by Wiki 2.0 explicit, idempotent, and
safe for the existing synchronous ingest path.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text
from sqlmodel import SQLModel

WIKI_MIGRATION_BACKUP_SUFFIX = ".wiki2-pre-migration.bak"

# SQLite ADD COLUMN definitions are deliberately nullable or have a literal
# default so old rows remain readable while the process is being upgraded.
WIKI_PAGE_COLUMNS: dict[str, str] = {
    "page_key": "VARCHAR",
    "domain": "VARCHAR",
    "status": "VARCHAR DEFAULT 'published'",
    "revision": "INTEGER DEFAULT 1",
    "aliases_json": "TEXT DEFAULT '[]'",
    "content_hash": "VARCHAR",
}

INGEST_JOB_COLUMNS: dict[str, str] = {
    "stage": "VARCHAR DEFAULT 'queued'",
    "progress": "INTEGER DEFAULT 0",
    "plan_json": "TEXT DEFAULT '{}'",
    "model_ref": "VARCHAR",
    "prompt_version_ref": "VARCHAR",
    "cancel_requested": "BOOLEAN DEFAULT 0",
}

SOURCE_CHUNK_COLUMNS: dict[str, str] = {
    "page_start": "INTEGER",
    "page_end": "INTEGER",
    "section": "TEXT DEFAULT ''",
    "clause_ids_json": "TEXT DEFAULT '[]'",
    "parent_index": "INTEGER",
}


def migration_backup_path(db_path: Path | str) -> Path:
    """Return the stable, recognizable backup path for one SQLite database."""

    path = Path(db_path)
    return path.with_name(path.name + WIKI_MIGRATION_BACKUP_SUFFIX)


def _database_path(engine: Engine) -> Path | None:
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database)


def _table_exists(conn: Any, table_name: str) -> bool:
    result = conn.execute(
        text(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = :table_name"
        ),
        {"table_name": table_name},
    ).first()
    return result is not None


def _columns(conn: Any, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    rows = conn.execute(text(f'PRAGMA table_info("{table_name}")')).fetchall()
    return {str(row[1]) for row in rows}


def _index_names(conn: Any, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    rows = conn.execute(text(f'PRAGMA index_list("{table_name}")')).fetchall()
    return {str(row[1]) for row in rows}


def _wiki_migration_needed(conn: Any) -> bool:
    """Detect an old or partially migrated schema before any DDL runs."""

    page_columns = _columns(conn, "wiki_pages")
    if page_columns:
        if set(WIKI_PAGE_COLUMNS) - page_columns:
            return True
        page_key_index = "uq_wiki_pages_page_key_not_null"
        if page_key_index not in _index_names(conn, "wiki_pages"):
            return True
        has_legacy_keys = conn.execute(
            text(
                "SELECT 1 FROM wiki_pages "
                "WHERE page_key IS NULL OR trim(page_key) = '' LIMIT 1"
            )
        ).first()
        if has_legacy_keys is not None:
            return True

    job_columns = _columns(conn, "ingest_jobs")
    if job_columns and set(INGEST_JOB_COLUMNS) - job_columns:
        return True

    source_chunk_columns = _columns(conn, "source_chunks")
    if source_chunk_columns and set(SOURCE_CHUNK_COLUMNS) - source_chunk_columns:
        return True

    # A partially migrated database may already have all added columns while
    # still missing the new Wiki 2.0 relation/audit tables.  Treat that as a
    # migration too so a backup is taken before create_all changes the schema.
    if page_columns or job_columns:
        required_tables = {
            "wiki_page_sources",
            "wiki_page_revisions",
            "wiki_review_items",
        }
        if any(not _table_exists(conn, name) for name in required_tables):
            return True

    return False


def backup_before_wiki_migration(engine: Engine) -> Path | None:
    """Create one non-overwriting backup if this DB needs Wiki migration.

    New databases do not need a backup.  Once the canonical backup exists,
    rerunning startup returns that path without replacing it.
    """

    db_path = _database_path(engine)
    if db_path is None or not db_path.exists():
        return None

    with engine.connect() as conn:
        needed = _wiki_migration_needed(conn)
    if not needed:
        return None

    backup_path = migration_backup_path(db_path)
    if backup_path.exists():
        return backup_path

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    # ``xb`` prevents a concurrent initializer or a user-created backup from
    # being overwritten.  The database is copied before any migration DDL.
    with db_path.open("rb") as source, backup_path.open("xb") as target:
        shutil.copyfileobj(source, target)
    return backup_path


def _add_missing_columns(engine: Engine) -> None:
    with engine.begin() as conn:
        for table_name, definitions in (
            ("wiki_pages", WIKI_PAGE_COLUMNS),
            ("ingest_jobs", INGEST_JOB_COLUMNS),
            ("source_chunks", SOURCE_CHUNK_COLUMNS),
        ):
            existing = _columns(conn, table_name)
            if not existing:
                continue
            for column_name, definition in definitions.items():
                if column_name in existing:
                    continue
                conn.execute(
                    text(
                        f'ALTER TABLE "{table_name}" '
                        f'ADD COLUMN "{column_name}" {definition}'
                    )
                )
                existing.add(column_name)


def _legacy_page_key(conn: Any, page_id: int) -> str:
    """Build a valid deterministic key, avoiding a user-supplied collision."""

    base = f"legacy.page.{page_id}"
    candidate = base
    suffix = 1
    while (
        conn.execute(
            text(
                "SELECT 1 FROM wiki_pages "
                "WHERE page_key = :page_key AND id <> :page_id LIMIT 1"
            ),
            {"page_key": candidate, "page_id": page_id},
        ).first()
        is not None
    ):
        candidate = f"{base}.legacy{suffix}"
        suffix += 1
    return candidate


def _backfill_legacy_rows(engine: Engine) -> None:
    with engine.begin() as conn:
        if _table_exists(conn, "wiki_pages"):
            conn.execute(
                text(
                    "UPDATE wiki_pages SET status = 'published' "
                    "WHERE status IS NULL OR trim(status) = ''"
                )
            )
            conn.execute(
                text(
                    "UPDATE wiki_pages SET revision = 1 "
                    "WHERE revision IS NULL OR revision < 1"
                )
            )
            conn.execute(
                text(
                    "UPDATE wiki_pages SET aliases_json = '[]' "
                    "WHERE aliases_json IS NULL OR trim(aliases_json) = ''"
                )
            )
            rows = conn.execute(
                text(
                    "SELECT id FROM wiki_pages "
                    "WHERE page_key IS NULL OR trim(page_key) = '' "
                    "ORDER BY id"
                )
            ).fetchall()
            for row in rows:
                page_id = int(row[0])
                conn.execute(
                    text(
                        "UPDATE wiki_pages SET page_key = :page_key "
                        "WHERE id = :page_id "
                        "AND (page_key IS NULL OR trim(page_key) = '')"
                    ),
                    {
                        "page_key": _legacy_page_key(conn, page_id),
                        "page_id": page_id,
                    },
                )

        if _table_exists(conn, "ingest_jobs"):
            conn.execute(
                text(
                    "UPDATE ingest_jobs SET plan_json = '{}' "
                    "WHERE plan_json IS NULL OR trim(plan_json) = ''"
                )
            )
            conn.execute(
                text(
                    "UPDATE ingest_jobs SET cancel_requested = 0 "
                    "WHERE cancel_requested IS NULL"
                )
            )
            conn.execute(
                text(
                    "UPDATE ingest_jobs SET progress = 100 "
                    "WHERE progress IS NULL AND status IN ('success', 'ready')"
                )
            )
            conn.execute(
                text(
                    "UPDATE ingest_jobs SET stage = 'ready' "
                    "WHERE (stage IS NULL OR trim(stage) = '' OR stage = 'queued') "
                    "AND status IN ('success', 'ready')"
                )
            )
            conn.execute(
                text(
                    "UPDATE ingest_jobs SET stage = 'failed' "
                    "WHERE (stage IS NULL OR trim(stage) = '' OR stage = 'queued') "
                    "AND status = 'failed'"
                )
            )
            conn.execute(
                text(
                    "UPDATE ingest_jobs SET stage = 'applying' "
                    "WHERE (stage IS NULL OR trim(stage) = '' OR stage = 'queued') "
                    "AND status = 'running'"
                )
            )

        if _table_exists(conn, "source_chunks"):
            conn.execute(
                text(
                    "UPDATE source_chunks SET section = '' "
                    "WHERE section IS NULL"
                )
            )
            conn.execute(
                text(
                    "UPDATE source_chunks SET clause_ids_json = '[]' "
                    "WHERE clause_ids_json IS NULL OR trim(clause_ids_json) = ''"
                )
            )


def _create_legacy_and_query_indexes(engine: Engine) -> None:
    """Create indexes that SQLModel cannot add to an already existing table."""

    statements = (
        (
            "wiki_pages",
            'CREATE INDEX IF NOT EXISTS "ix_wiki_pages_domain" '
            'ON "wiki_pages" ("domain")',
        ),
        (
            "wiki_pages",
            'CREATE INDEX IF NOT EXISTS "ix_wiki_pages_status" '
            'ON "wiki_pages" ("status")',
        ),
        (
            "wiki_pages",
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_wiki_pages_page_key_not_null" '
            'ON "wiki_pages" ("page_key") WHERE "page_key" IS NOT NULL',
        ),
        (
            "ingest_jobs",
            'CREATE INDEX IF NOT EXISTS "ix_ingest_jobs_document_id" '
            'ON "ingest_jobs" ("document_id")',
        ),
        (
            "ingest_jobs",
            'CREATE INDEX IF NOT EXISTS "ix_ingest_jobs_status" '
            'ON "ingest_jobs" ("status")',
        ),
        (
            "ingest_jobs",
            'CREATE INDEX IF NOT EXISTS "ix_ingest_jobs_stage" '
            'ON "ingest_jobs" ("stage")',
        ),
    )
    with engine.begin() as conn:
        for table_name, statement in statements:
            if _table_exists(conn, table_name):
                conn.execute(text(statement))


def migrate_wiki_schema(engine: Engine) -> Path | None:
    """Back up, create, alter, backfill, and index the Wiki 2.0 schema.

    The function is safe to call on every application startup.  It does not
    move or rewrite Markdown files; legacy rows retain their existing id and
    path and only receive database metadata needed by the new model.
    """

    backup_path = backup_before_wiki_migration(engine)
    _add_missing_columns(engine)

    # Importing entities here also makes the helper safe to call directly in a
    # migration script before app.db has imported the model module.
    from app.models import entities  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _backfill_legacy_rows(engine)
    _create_legacy_and_query_indexes(engine)
    return backup_path
