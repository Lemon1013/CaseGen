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

from app import config

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

SPACE_COLUMNS: dict[str, dict[str, str]] = {
    "documents": {"space_id": "INTEGER"},
    "ingest_jobs": {"space_id": "INTEGER"},
    "source_chunks": {"space_id": "INTEGER"},
    "wiki_pages": {"space_id": "INTEGER"},
    "wiki_review_items": {"space_id": "INTEGER"},
    "generation_tasks": {"wiki_space_id": "INTEGER"},
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

    # Space isolation changes both canonical tables and the rebuildable FTS
    # projection.  A missing space table/column is therefore a migration even
    # when the database already contains every Wiki 2.0 column.
    if not _table_exists(conn, "wiki_spaces"):
        return True
    for table_name, definitions in SPACE_COLUMNS.items():
        if _table_exists(conn, table_name) and set(definitions) - _columns(conn, table_name):
            return True

    page_columns = _columns(conn, "wiki_pages")
    if page_columns:
        if set(WIKI_PAGE_COLUMNS) - page_columns:
            return True
        indexes = _index_names(conn, "wiki_pages")
        page_key_index = "uq_wiki_pages_space_page_key"
        if page_key_index not in indexes:
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

    # FTS5 virtual tables cannot be ALTERed.  Inspect their projected columns
    # before any startup DDL so the database is backed up before a drop/rebuild.
    for table_name, required in (
        ("wiki_pages_fts", {"space_id"}),
        ("source_chunks_fts", {"space_id"}),
    ):
        if _table_exists(conn, table_name):
            columns = _columns(conn, table_name)
            if not required.issubset(columns):
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
            *SPACE_COLUMNS.items(),
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


def _ensure_default_space(engine: Engine) -> int:
    """Create the compatibility space after ``wiki_spaces`` exists."""

    with engine.begin() as conn:
        row = conn.execute(
            text('SELECT id FROM "wiki_spaces" WHERE slug = :slug'),
            {"slug": "default"},
        ).first()
        if row is None:
            conn.execute(
                text(
                    'INSERT INTO "wiki_spaces" '
                    '(name, slug, description, status, created_at, updated_at) '
                    'VALUES (:name, :slug, :description, :status, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)'
                ),
                {
                    "name": "默认空间",
                    "slug": "default",
                    "description": "由系统迁移和兼容旧 Wiki 数据使用的默认空间",
                    "status": "active",
                },
            )
            row = conn.execute(
                text('SELECT id FROM "wiki_spaces" WHERE slug = :slug'),
                {"slug": "default"},
            ).first()
        if row is None:
            raise RuntimeError("could not create the default Wiki space")
        return int(row[0])


def _backfill_space_columns(engine: Engine, default_space_id: int) -> None:
    """Backfill every legacy row to the default space, preserving snapshots."""

    with engine.begin() as conn:
        if _table_exists(conn, "documents"):
            conn.execute(
                text(
                    "UPDATE documents SET space_id = COALESCE(space_id, :space_id)"
                ),
                {"space_id": default_space_id},
            )
        if _table_exists(conn, "ingest_jobs"):
            conn.execute(
                text(
                    "UPDATE ingest_jobs SET space_id = COALESCE("
                    "space_id, "
                    "(SELECT space_id FROM documents WHERE documents.id = ingest_jobs.document_id), "
                    ":space_id)"
                ),
                {"space_id": default_space_id},
            )
        if _table_exists(conn, "wiki_pages"):
            conn.execute(
                text(
                    "UPDATE wiki_pages SET space_id = COALESCE("
                    "space_id, "
                    "(SELECT space_id FROM documents WHERE documents.id = wiki_pages.source_document_id), "
                    ":space_id)"
                ),
                {"space_id": default_space_id},
            )
        if _table_exists(conn, "source_chunks"):
            conn.execute(
                text(
                    "UPDATE source_chunks SET space_id = COALESCE("
                    "space_id, "
                    "(SELECT space_id FROM documents WHERE documents.id = source_chunks.document_id), "
                    ":space_id)"
                ),
                {"space_id": default_space_id},
            )
        if _table_exists(conn, "wiki_review_items"):
            conn.execute(
                text(
                    "UPDATE wiki_review_items SET space_id = COALESCE("
                    "space_id, "
                    "(SELECT space_id FROM wiki_pages WHERE wiki_pages.id = wiki_review_items.page_id), "
                    "(SELECT space_id FROM ingest_jobs WHERE ingest_jobs.id = wiki_review_items.job_id), "
                    ":space_id)"
                ),
                {"space_id": default_space_id},
            )
        if _table_exists(conn, "generation_tasks"):
            conn.execute(
                text(
                    "UPDATE generation_tasks SET wiki_space_id = COALESCE(wiki_space_id, :space_id)"
                ),
                {"space_id": default_space_id},
            )


def _move_legacy_wiki_files(engine: Engine, default_space_id: int) -> None:
    """Move existing formal files into ``spaces/default`` when present.

    Missing files are left untouched for compatibility with metadata-only
    legacy databases and are still reported by the existing Wiki lint checks.
    A same-content destination is treated as an already completed move.
    """

    if not config.WIKI_DIR.exists() or not _table_exists_from_engine(engine, "wiki_pages"):
        return
    changed: list[tuple[int, str, Path, Path]] = []
    root = Path(config.WIKI_DIR).resolve()
    target_root = (root / "spaces" / "default" / "pages").resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, path FROM wiki_pages "
                "WHERE space_id = :space_id AND path IS NOT NULL "
                "AND path NOT LIKE 'spaces/%'"
            ),
            {"space_id": default_space_id},
        ).fetchall()
    for row in rows:
        page_id, raw_path = int(row[0]), str(row[1] or "")
        source = Path(raw_path)
        if not source.is_absolute():
            source = root / source
        try:
            source = source.resolve()
            relative = source.relative_to(root)
        except (OSError, ValueError):
            continue
        if not source.is_file():
            continue
        # Keep the legacy relative hierarchy under the new per-space pages
        # root; strip the old top-level ``pages`` wrapper when present.
        if relative.parts and relative.parts[0].lower() == "pages":
            relative = Path(*relative.parts[1:])
        if not relative.parts:
            continue
        destination = (target_root / relative).resolve()
        try:
            destination.relative_to(target_root)
        except ValueError:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != source.read_bytes():
                # Never overwrite an unrelated file during startup migration.
                continue
        else:
            # Copy first: the legacy file remains recoverable until the DB
            # transaction has committed the new relative path.
            shutil.copy2(str(source), str(destination))
        changed.append((page_id, destination.relative_to(root).as_posix(), source, destination))
    if changed:
        with engine.begin() as conn:
            for page_id, path, _source, _destination in changed:
                conn.execute(
                    text("UPDATE wiki_pages SET path = :path WHERE id = :page_id"),
                    {"path": path, "page_id": page_id},
                )
        # Cleanup happens only after the database points at every destination.
        # A failed unlink merely leaves a harmless duplicate for the next run.
        for _page_id, _path, source, destination in changed:
            if source != destination:
                try:
                    source.unlink()
                except OSError:
                    pass


def _table_exists_from_engine(engine: Engine, table_name: str) -> bool:
    with engine.connect() as conn:
        return _table_exists(conn, table_name)


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


def _backfill_legacy_rows(engine: Engine, default_space_id: int | None = None) -> None:
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
    if default_space_id is not None:
        _backfill_space_columns(engine, default_space_id)


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
            'CREATE INDEX IF NOT EXISTS "uq_wiki_pages_page_key_not_null" '
            'ON "wiki_pages" ("page_key") WHERE "page_key" IS NOT NULL',
        ),
        (
            "wiki_pages",
            'CREATE UNIQUE INDEX IF NOT EXISTS "uq_wiki_pages_space_page_key" '
            'ON "wiki_pages" ("space_id", "page_key") WHERE "page_key" IS NOT NULL',
        ),
        (
            "wiki_pages",
            'CREATE INDEX IF NOT EXISTS "ix_wiki_pages_space_status" '
            'ON "wiki_pages" ("space_id", "status")',
        ),
        (
            "wiki_pages",
            'CREATE INDEX IF NOT EXISTS "ix_wiki_pages_space_document" '
            'ON "wiki_pages" ("space_id", "source_document_id")',
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
        (
            "ingest_jobs",
            'CREATE INDEX IF NOT EXISTS "ix_ingest_jobs_space_document" '
            'ON "ingest_jobs" ("space_id", "document_id")',
        ),
        (
            "source_chunks",
            'CREATE INDEX IF NOT EXISTS "ix_source_chunks_space_document" '
            'ON "source_chunks" ("space_id", "document_id")',
        ),
        (
            "wiki_review_items",
            'CREATE INDEX IF NOT EXISTS "ix_wiki_review_items_space_status" '
            'ON "wiki_review_items" ("space_id", "status")',
        ),
        (
            "documents",
            'CREATE INDEX IF NOT EXISTS "ix_documents_space_status" '
            'ON "documents" ("space_id", "status")',
        ),
        (
            "generation_tasks",
            'CREATE INDEX IF NOT EXISTS "ix_generation_tasks_wiki_space" '
            'ON "generation_tasks" ("wiki_space_id")',
        ),
    )
    with engine.begin() as conn:
        # Older releases created a global unique page_key index.  Drop it
        # before creating the scoped key; retain the old name as a normal
        # lookup index for tooling/tests that know the historical name.
        if _table_exists(conn, "wiki_pages"):
            conn.execute(text('DROP INDEX IF EXISTS "uq_wiki_pages_page_key_not_null"'))
        for table_name, statement in statements:
            if _table_exists(conn, table_name):
                conn.execute(text(statement))


def _migrate_fts_projection(engine: Engine) -> None:
    """Drop/recreate old FTS virtual tables; FTS5 has no usable ALTER path."""

    with engine.begin() as conn:
        needs_rebuild = False
        for table_name in ("wiki_pages_fts", "source_chunks_fts"):
            if not _table_exists(conn, table_name):
                needs_rebuild = True
                continue
            columns = _columns(conn, table_name)
            if "space_id" not in columns:
                needs_rebuild = True
        if needs_rebuild:
            # Dropping the virtual table also drops its FTS5 shadow tables.
            # Both projections are rebuilt together so counts cannot describe
            # a mixed old/new isolation model.
            conn.execute(text('DROP TABLE IF EXISTS "wiki_pages_fts"'))
            conn.execute(text('DROP TABLE IF EXISTS "source_chunks_fts"'))

    try:
        from app.services.wiki_fts import ensure_fts_schema, rebuild_fts

        status = ensure_fts_schema(engine)
        if status.available:
            rebuild_fts(engine)
    except Exception:
        # FTS is a rebuildable optimization.  Canonical rows and deterministic
        # retrieval remain available even when a platform lacks FTS5.
        return


def migrate_wiki_schema(engine: Engine) -> Path | None:
    """Back up, create, alter, backfill, and index the Wiki 2.0 schema.

    The function is safe to call on every application startup. Legacy rows
    retain their ids; legacy Markdown files are moved once into the default
    space and their stored paths are updated to the scoped layout.
    """

    backup_path = backup_before_wiki_migration(engine)
    _add_missing_columns(engine)

    # Importing entities here also makes the helper safe to call directly in a
    # migration script before app.db has imported the model module.
    from app.models import entities  # noqa: F401

    SQLModel.metadata.create_all(engine)
    default_space_id = _ensure_default_space(engine)
    _backfill_legacy_rows(engine, default_space_id)
    _move_legacy_wiki_files(engine, default_space_id)
    _create_legacy_and_query_indexes(engine)
    _migrate_fts_projection(engine)
    try:
        from app.services.wiki_spaces import ensure_space_dirs
        from sqlmodel import Session

        with Session(engine) as session:
            row = session.get(entities.WikiSpace, default_space_id)
            if row is not None:
                ensure_space_dirs(row)
    except Exception:
        # Directory initialization is retried by config/space services.
        pass
    return backup_path
