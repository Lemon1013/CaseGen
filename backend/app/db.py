from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine
from app import config
from app.config import ensure_data_dirs

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        ensure_data_dirs()
        _engine = create_engine(
            f"sqlite:///{config.DB_PATH}",
            connect_args={"check_same_thread": False},
        )
    return _engine


def _migrate_sqlite_columns(engine) -> None:
    """Add columns introduced after first deploy (SQLite has no auto-alter)."""
    with engine.begin() as conn:
        try:
            cols = conn.execute(text("PRAGMA table_info(task_citations)")).fetchall()
        except Exception:  # table may not exist yet
            return
        names = {row[1] for row in cols}
        alters: list[str] = []
        if "citation_type" not in names:
            alters.append(
                "ALTER TABLE task_citations ADD COLUMN citation_type VARCHAR DEFAULT 'wiki'"
            )
        if "source_chunk_id" not in names:
            alters.append(
                "ALTER TABLE task_citations ADD COLUMN source_chunk_id INTEGER"
            )
        if "content_excerpt" not in names:
            alters.append(
                "ALTER TABLE task_citations ADD COLUMN content_excerpt TEXT DEFAULT ''"
            )
        if "clause_ids_json" not in names:
            alters.append(
                "ALTER TABLE task_citations ADD COLUMN clause_ids_json TEXT DEFAULT '[]'"
            )
        if "anchor_clause" not in names:
            alters.append(
                "ALTER TABLE task_citations ADD COLUMN anchor_clause VARCHAR"
            )
        for sql in alters:
            conn.execute(text(sql))


def _migrate_model_defaults(engine) -> None:
    """Normalize legacy duplicate defaults and install the DB constraint."""
    with engine.begin() as conn:
        columns = conn.execute(text('PRAGMA table_info("models")')).fetchall()
        names = {str(row[1]) for row in columns}
        if not columns or "is_default" not in names:
            return

        defaults = conn.execute(
            text(
                'SELECT id FROM "models" '
                'WHERE is_default = 1 ORDER BY id DESC'
            )
        ).fetchall()
        if len(defaults) > 1:
            keep_id = int(defaults[0][0])
            conn.execute(
                text(
                    'UPDATE "models" SET is_default = 0 '
                    'WHERE is_default = 1 AND id <> :keep_id'
                ),
                {"keep_id": keep_id},
            )

        conn.execute(
            text(
                'CREATE UNIQUE INDEX IF NOT EXISTS "uq_models_single_default" '
                'ON "models" ("is_default") WHERE "is_default" = 1'
            )
        )


def init_db() -> None:
    from app.models import entities  # noqa: F401
    from app.services.wiki_migrate import backup_before_wiki_migration, migrate_wiki_schema

    engine = get_engine()
    # Clean legacy duplicate defaults before SQLModel.metadata.create_all
    # attempts to create the partial unique index on an existing database.
    backup_before_wiki_migration(engine)
    _migrate_model_defaults(engine)
    # Wiki migration performs its backup before any create/alter/backfill
    # operation.  It also calls create_all so the new Wiki tables are present
    # before the remaining compatibility migration runs.
    migrate_wiki_schema(engine)
    _migrate_sqlite_columns(engine)
    _migrate_model_defaults(engine)


def get_session():
    with Session(get_engine()) as session:
        yield session


def reset_engine() -> None:
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None
