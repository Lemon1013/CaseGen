import shutil
import logging
import sqlite3
from pathlib import Path

from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine
from app import config
from app.config import ensure_data_dirs

logger = logging.getLogger(__name__)

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
            cols = []
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

        checkpoint_cols = conn.execute(text("PRAGMA table_info(task_retrieval_checkpoints)")).fetchall()
        if checkpoint_cols:
            checkpoint_names = {row[1] for row in checkpoint_cols}
            if "auto_review" not in checkpoint_names:
                conn.execute(text("ALTER TABLE task_retrieval_checkpoints ADD COLUMN auto_review BOOLEAN NOT NULL DEFAULT 0"))
            if "resume_claim_token" not in checkpoint_names:
                conn.execute(text("ALTER TABLE task_retrieval_checkpoints ADD COLUMN resume_claim_token VARCHAR"))
            if "resume_claimed_at" not in checkpoint_names:
                conn.execute(text("ALTER TABLE task_retrieval_checkpoints ADD COLUMN resume_claimed_at DATETIME"))
            if "resume_started_at" not in checkpoint_names:
                conn.execute(text("ALTER TABLE task_retrieval_checkpoints ADD COLUMN resume_started_at DATETIME"))
            if "resume_status" not in checkpoint_names:
                conn.execute(text("ALTER TABLE task_retrieval_checkpoints ADD COLUMN resume_status VARCHAR"))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS task_retrieval_checkpoints (
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                version INTEGER NOT NULL DEFAULT 1,
                status VARCHAR NOT NULL DEFAULT 'pending',
                auto_review BOOLEAN NOT NULL DEFAULT 0,
                resume_claim_token VARCHAR,
                resume_claimed_at DATETIME,
                resume_started_at DATETIME,
                resume_status VARCHAR,
                query TEXT NOT NULL DEFAULT '',
                retrieval_json TEXT NOT NULL DEFAULT '{}',
                candidate_citation_ids_json TEXT NOT NULL DEFAULT '[]',
                selected_citation_ids_json TEXT NOT NULL DEFAULT '[]',
                supplemental_text TEXT NOT NULL DEFAULT '',
                decision_hash VARCHAR,
                idempotency_key VARCHAR,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                FOREIGN KEY(task_id) REFERENCES generation_tasks(id)
            )
        """))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_task_retrieval_checkpoint_attempt ON task_retrieval_checkpoints(task_id, attempt)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_task_retrieval_checkpoint_task_status ON task_retrieval_checkpoints(task_id, status)"))


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


def _case_migration_backup_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".case-management-pre-migration.bak")


def _scrub_case_log_bodies(db_path: Path) -> None:
    """Destroy reversible body snapshots in a live DB or migration backup.

    Legacy log columns are intentionally kept for SQLite shape compatibility,
    but their values must not remain recoverable from either the live database
    or a migration backup.  ``VACUUM`` rewrites the SQLite file after the
    update, so stale body bytes are not retained in free pages.
    """

    if not db_path.is_file():
        raise RuntimeError(
            f"Case-management migration database to sanitize does not exist: {db_path}"
        )

    try:
        with sqlite3.connect(db_path) as conn:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='test_case_operation_logs'"
            ).fetchone()
            if table_exists is None:
                return
            columns = {
                str(row[1])
                for row in conn.execute(
                    'PRAGMA table_info("test_case_operation_logs")'
                ).fetchall()
            }
            clear_values = {
                "before_content_md": "NULL",
                "after_content_md": "NULL",
                "diff_text": "''",
                "diff_json": "'{}'",
            }
            assignments = [
                f'"{name}" = {value}'
                for name, value in clear_values.items()
                if name in columns
            ]
            if not assignments:
                return

            # Secure delete covers overflow/free cells from the UPDATE; VACUUM
            # then rebuilds the file without the old payloads.
            conn.execute("PRAGMA secure_delete = ON")
            conn.execute(
                'UPDATE "test_case_operation_logs" SET ' + ", ".join(assignments)
            )
            conn.commit()
            conn.execute("VACUUM")
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"Case-management migration could not sanitize reversible operation-log data: {db_path}"
        ) from exc


def _sanitize_case_migration_databases(db_path: Path, backup_path: Path) -> None:
    """Sanitize both migration copies, reporting every failed target clearly."""

    failures: list[str] = []
    for label, path in (("live database", db_path), ("migration backup", backup_path)):
        try:
            _scrub_case_log_bodies(path)
        except Exception as exc:
            failures.append(f"{label} ({path}): {exc}")
    if failures:
        raise RuntimeError(
            "Case-management migration could not sanitize reversible operation-log data in "
            + "; ".join(failures)
        )


def _backup_before_case_migration(engine) -> None:
    """Back up before case DDL/data cleanup and preflight duplicate conflicts.

    ``migrate_wiki_schema`` invokes ``SQLModel.metadata.create_all`` later in
    startup.  That call may create the case unique indexes before the case
    compatibility migration gets a chance to inspect rows, so duplicate
    checks must happen here, before *any* related DDL.  Existing conflicting
    rows are left untouched and startup stops with a diagnostic error after a
    non-overwriting snapshot is made.
    """

    database = engine.url.database
    if not database or database == ":memory:":
        return
    db_path = Path(database)
    if not db_path.exists():
        return
    with engine.connect() as conn:
        rows = conn.execute(text('PRAGMA table_info("generation_tasks")')).fetchall()
        names = {str(row[1]) for row in rows}
        tables = conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ).fetchall()
        # A brand-new empty SQLite file needs no pre-migration snapshot.
        if not tables:
            return
        generation_needs = bool(rows) and ({"finalized_draft_id", "finalized_at"} - names)
        test_cases_exists = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='test_cases'"
            )
        ).first() is not None
        logs_exists = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='test_case_operation_logs'"
            )
        ).first() is not None
        log_columns = {
            str(row[1])
            for row in conn.execute(
                text('PRAGMA table_info("test_case_operation_logs")')
            ).fetchall()
        } if logs_exists else set()
        reversible_log_columns = {
            "before_content_md",
            "after_content_md",
            "diff_text",
            "diff_json",
        } & log_columns
        audit_metadata_columns = {
            "changed_fields_json",
            "before_hash",
            "after_hash",
            "before_length",
            "after_length",
            "added_lines",
            "deleted_lines",
            "title_changed",
            "diff_summary",
        }
        test_case_columns = {
            str(row[1])
            for row in conn.execute(text('PRAGMA table_info("test_cases")')).fetchall()
        } if test_cases_exists else set()
        indexes = {
            str(row[1])
            for row in conn.execute(text('PRAGMA index_list("test_cases")')).fetchall()
        } if test_cases_exists else set()

        source_duplicates = []
        normalized_duplicates = []
        invalid_case_keys = []
        if test_cases_exists:
            if {
                "source_task_id",
                "source_draft_id",
                "source_case_key",
            }.issubset(test_case_columns):
                source_duplicates = conn.execute(
                    text(
                        'SELECT "source_task_id", "source_draft_id", "source_case_key", COUNT(*) AS n '
                        'FROM "test_cases" '
                        'WHERE "source_task_id" IS NOT NULL '
                        'AND "source_draft_id" IS NOT NULL '
                        'AND "source_case_key" IS NOT NULL '
                        'GROUP BY "source_task_id", "source_draft_id", "source_case_key" '
                        'HAVING n > 1 LIMIT 5'
                    )
                ).fetchall()
            if {"requirement_id", "case_key"}.issubset(test_case_columns):
                normalized_duplicates = conn.execute(
                    text(
                        'SELECT "requirement_id", lower(trim("case_key", char(32, 9, 10, 13))), COUNT(*) AS n '
                        'FROM "test_cases" '
                        'WHERE "case_key" IS NOT NULL '
                        'AND trim("case_key", char(32, 9, 10, 13)) <> \'\' '
                        'GROUP BY "requirement_id", lower(trim("case_key", char(32, 9, 10, 13))) '
                        'HAVING n > 1 LIMIT 5'
                    )
                ).fetchall()
            if "case_key" in test_case_columns:
                invalid_case_keys = conn.execute(
                    text(
                        'SELECT "id", "requirement_id", "case_key" '
                        'FROM "test_cases" '
                        'WHERE "case_key" IS NULL '
                        'OR trim("case_key", char(32, 9, 10, 13)) = \'\' '
                        'LIMIT 5'
                    )
                ).fetchall()

        needs = (
            bool(generation_needs)
            or not test_cases_exists
            or not logs_exists
            or bool(reversible_log_columns)
            or bool(audit_metadata_columns - log_columns)
            or "uq_test_cases_source_identity" not in indexes
            or "uq_test_cases_requirement_case_key_normalized" not in indexes
            or bool(source_duplicates)
            or bool(normalized_duplicates)
            or bool(invalid_case_keys)
        )
    if not needs:
        return
    target = _case_migration_backup_path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        try:
            with db_path.open("rb") as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination)
        except FileExistsError:
            # Another process completed the same safe backup between the check
            # and opening the target. Never overwrite that snapshot.
            pass
    # This is intentionally before preflight errors are raised.  A blocked
    # startup must not leave reversible body snapshots in either the live DB
    # or its non-overwriting migration backup.
    _sanitize_case_migration_databases(db_path, target)

    if invalid_case_keys or source_duplicates or normalized_duplicates:
        details = []
        if invalid_case_keys:
            details.append(
                "invalid legacy case_key rows="
                + repr([tuple(row) for row in invalid_case_keys])
            )
        if source_duplicates:
            details.append(
                "source identity duplicates="
                + repr([tuple(row) for row in source_duplicates])
            )
        if normalized_duplicates:
            details.append(
                "requirement/case_key duplicates="
                + repr([tuple(row) for row in normalized_duplicates])
            )
        if invalid_case_keys and (source_duplicates or normalized_duplicates):
            problem = "invalid legacy case_key values and duplicate test cases"
        elif invalid_case_keys:
            problem = "invalid legacy case_key values"
        else:
            problem = "duplicate test cases"
        raise RuntimeError(
            f"Case-management migration blocked by {problem}; "
            f"backup saved at {target}: {'; '.join(details)}"
        )


def _migrate_case_management_schema(engine) -> None:
    """Add case-management columns/indexes to databases created by older builds."""

    with engine.begin() as conn:
        if conn.execute(
            text(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='generation_tasks'"
            )
        ).first():
            columns = {
                str(row[1])
                for row in conn.execute(text('PRAGMA table_info("generation_tasks")')).fetchall()
            }
            if "finalized_draft_id" not in columns:
                conn.execute(
                    text(
                        'ALTER TABLE "generation_tasks" '
                        'ADD COLUMN "finalized_draft_id" INTEGER'
                    )
                )
            if "finalized_at" not in columns:
                conn.execute(
                    text(
                        'ALTER TABLE "generation_tasks" '
                        'ADD COLUMN "finalized_at" DATETIME'
                    )
                )

        # SQLModel creates these tables on a fresh database.  The CREATE INDEX
        # statements are deliberately conditional for partially migrated DBs.
        conn.execute(
            text(
                'CREATE INDEX IF NOT EXISTS "ix_generation_tasks_finalized_draft_id" '
                'ON "generation_tasks" ("finalized_draft_id")'
            )
        )
        if conn.execute(
            text(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='test_cases'"
            )
        ).first():
            conn.execute(
                text(
                    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_test_cases_source_identity" '
                    'ON "test_cases" ("source_task_id", "source_draft_id", "source_case_key")'
                )
            )

            # A previous local build stored reversible body snapshots in the
            # operation-log table.  Keep legacy columns for SQLite shape
            # compatibility, but destroy their values and add only
            # non-reversible metadata for all new writes. SQLModel's create_all
            # handles fresh databases; these ALTERs cover an existing one.
            log_columns = {
                str(row[1])
                for row in conn.execute(
                    text('PRAGMA table_info("test_case_operation_logs")')
                ).fetchall()
            }
            log_alters = {
                "changed_fields_json": 'ALTER TABLE "test_case_operation_logs" ADD COLUMN "changed_fields_json" TEXT DEFAULT \'[]\'',
                "before_hash": 'ALTER TABLE "test_case_operation_logs" ADD COLUMN "before_hash" VARCHAR',
                "after_hash": 'ALTER TABLE "test_case_operation_logs" ADD COLUMN "after_hash" VARCHAR',
                "before_length": 'ALTER TABLE "test_case_operation_logs" ADD COLUMN "before_length" INTEGER',
                "after_length": 'ALTER TABLE "test_case_operation_logs" ADD COLUMN "after_length" INTEGER',
                "added_lines": 'ALTER TABLE "test_case_operation_logs" ADD COLUMN "added_lines" INTEGER DEFAULT 0',
                "deleted_lines": 'ALTER TABLE "test_case_operation_logs" ADD COLUMN "deleted_lines" INTEGER DEFAULT 0',
                "title_changed": 'ALTER TABLE "test_case_operation_logs" ADD COLUMN "title_changed" BOOLEAN DEFAULT 0',
                "diff_summary": 'ALTER TABLE "test_case_operation_logs" ADD COLUMN "diff_summary" TEXT DEFAULT \'\'',
            }
            if log_columns:
                for name, sql in log_alters.items():
                    if name not in log_columns:
                        conn.execute(text(sql))

                # Destructively clear body-bearing values left by the local
                # pre-release schema.  The migration backup above is the
                # recovery snapshot; the live database must no longer retain
                # reconstructable before/after content or unified diff text.
                clear_values = {
                    "before_content_md": "NULL",
                    "after_content_md": "NULL",
                    "diff_text": "''",
                    "diff_json": "'{}'",
                }
                assignments = [
                    f'"{name}" = {value}'
                    for name, value in clear_values.items()
                    if name in log_columns
                ]
                if assignments:
                    conn.execute(
                        text(
                            'UPDATE "test_case_operation_logs" SET '
                            + ", ".join(assignments)
                        )
                    )

            duplicates = conn.execute(
                text(
                    'SELECT "requirement_id", lower(trim("case_key", char(32, 9, 10, 13))), COUNT(*) AS n '
                    'FROM "test_cases" '
                    'GROUP BY "requirement_id", lower(trim("case_key", char(32, 9, 10, 13))) '
                    'HAVING n > 1 LIMIT 5'
                )
            ).fetchall()
            if duplicates:
                logger.warning(
                    "Skipping normalized test-case uniqueness index because legacy duplicates exist: %s",
                    [tuple(row) for row in duplicates],
                )
            else:
                conn.execute(
                    text(
                        'CREATE UNIQUE INDEX IF NOT EXISTS "uq_test_cases_requirement_case_key_normalized" '
                        'ON "test_cases" ("requirement_id", lower(trim("case_key")))'
                    )
                )
            conn.execute(
                text(
                    'CREATE INDEX IF NOT EXISTS "ix_test_cases_requirement_status_key" '
                    'ON "test_cases" ("requirement_id", "status", "case_key")'
                )
            )


def _migrate_test_design_schema(engine) -> None:
    """Upgrade the task/test-point schema for existing SQLite databases.

    ``SQLModel.metadata.create_all`` creates the new normalized tables, but it
    intentionally does not alter columns on tables that already exist.  Keep
    this migration additive and idempotent so old task rows remain readable
    with the standard strategy defaults.
    """

    with engine.begin() as conn:
        generation_columns = {
            str(row[1])
            for row in conn.execute(text('PRAGMA table_info("generation_tasks")')).fetchall()
        }
        if generation_columns:
            if "generation_granularity" not in generation_columns:
                conn.execute(
                    text(
                        'ALTER TABLE "generation_tasks" '
                        'ADD COLUMN "generation_granularity" VARCHAR NOT NULL DEFAULT \'standard\''
                    )
                )
            if "test_dimensions_json" not in generation_columns:
                conn.execute(
                    text(
                        'ALTER TABLE "generation_tasks" '
                        'ADD COLUMN "test_dimensions_json" TEXT NOT NULL '
                        'DEFAULT \'["positive", "negative", "boundary"]\''
                    )
                )

        case_columns = {
            str(row[1])
            for row in conn.execute(text('PRAGMA table_info("test_cases")')).fetchall()
        }
        if case_columns and "priority" not in case_columns:
            conn.execute(
                text(
                    'ALTER TABLE "test_cases" '
                    'ADD COLUMN "priority" VARCHAR NOT NULL DEFAULT \'P1\''
                )
            )
        if case_columns:
            conn.execute(
                text(
                    'CREATE INDEX IF NOT EXISTS "ix_test_cases_priority" '
                    'ON "test_cases" ("priority")'
                )
            )

        # Fresh databases already contain these tables from create_all.  The
        # conditional indexes also repair databases created by an intermediate
        # build where table creation succeeded but index creation did not.
        for table, column in (
            ("task_reference_cases", "task_id"),
            ("task_test_point_checkpoints", "task_id"),
            ("test_points", "task_id"),
            ("test_point_citations", "test_point_id"),
            ("draft_test_point_links", "draft_id"),
            ("test_point_case_links", "test_point_id"),
        ):
            exists = conn.execute(
                text(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:table_name"
                ),
                {"table_name": table},
            ).first()
            if exists:
                conn.execute(
                    text(
                        f'CREATE INDEX IF NOT EXISTS "ix_{table}_{column}" '
                        f'ON "{table}" ("{column}")'
                    )
                )

        point_checkpoint_exists = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='task_test_point_checkpoints'"
            )
        ).first()
        if point_checkpoint_exists:
            point_checkpoint_columns = {
                str(row[1])
                for row in conn.execute(
                    text('PRAGMA table_info("task_test_point_checkpoints")')
                ).fetchall()
            }
            if "auto_review" not in point_checkpoint_columns:
                conn.execute(
                    text(
                        'ALTER TABLE "task_test_point_checkpoints" '
                        'ADD COLUMN "auto_review" BOOLEAN NOT NULL DEFAULT 0'
                    )
                )


def init_db() -> None:
    from app.models import entities  # noqa: F401
    from app.services.wiki_migrate import backup_before_wiki_migration, migrate_wiki_schema

    engine = get_engine()
    _backup_before_case_migration(engine)
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
    _migrate_case_management_schema(engine)
    _migrate_test_design_schema(engine)


def get_session():
    with Session(get_engine()) as session:
        yield session


def reset_engine() -> None:
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None
