"""Tests for lightweight versioned SQLite schema migrations."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, text

from app.db import (
    VERSIONED_MIGRATIONS,
    _backup_db_before_versioned_migration,
    get_applied_migrations,
    get_engine,
    init_db,
    reset_engine,
    run_versioned_migrations,
)


def _expected_builtin_versions() -> list[str]:
    return sorted(version for version, _, _ in VERSIONED_MIGRATIONS)


def test_fresh_init_db_records_baseline_without_backup(tmp_app_data):
    db_path = tmp_app_data / "meta" / "app.db"
    assert not db_path.exists()

    init_db()

    applied = get_applied_migrations(get_engine())
    assert applied == _expected_builtin_versions()
    assert "0001_v2_refactor_baseline" in applied
    assert "0002_project_knowledge_sources" in applied
    assert list(db_path.parent.glob("*.pre-migration-*.bak")) == []


def test_repeated_init_db_is_idempotent(tmp_app_data):
    init_db()
    db_path = tmp_app_data / "meta" / "app.db"

    reset_engine()
    init_db()
    reset_engine()
    init_db()

    expected = _expected_builtin_versions()
    assert get_applied_migrations(get_engine()) == expected
    assert list(db_path.parent.glob("*.pre-migration-*.bak")) == []
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count == len(expected)


def test_custom_migration_creates_backup_runs_once_and_preserves_pre_state(tmp_app_data):
    init_db()
    db_path = tmp_app_data / "meta" / "app.db"
    engine = get_engine()

    # Seed pre-migration business table state
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE legacy_items (id INTEGER PRIMARY KEY, name TEXT)"))
        conn.execute(text("INSERT INTO legacy_items (id, name) VALUES (1, 'before')"))

    calls = 0

    def _add_col_migration(eng) -> None:
        nonlocal calls
        calls += 1
        with eng.begin() as conn:
            conn.execute(text("ALTER TABLE legacy_items ADD COLUMN note TEXT DEFAULT 'v2'"))

    migrations = (
        *VERSIONED_MIGRATIONS,
        ("9901_test_add_col", "Add note column to legacy_items", _add_col_migration),
    )

    applied = run_versioned_migrations(engine, migrations=migrations)
    assert applied == ["9901_test_add_col"]
    assert calls == 1
    assert get_applied_migrations(engine) == sorted([*_expected_builtin_versions(), "9901_test_add_col"])

    backup_path = db_path.with_name(f"{db_path.name}.pre-migration-9901_test_add_col.bak")
    assert backup_path.is_file()

    # Backup must reflect pre-migration state (without note column)
    with sqlite3.connect(backup_path) as bak_conn:
        bak_cols = {row[1] for row in bak_conn.execute("PRAGMA table_info(legacy_items)")}
        bak_rows = bak_conn.execute("SELECT id, name FROM legacy_items").fetchall()
    assert "note" not in bak_cols
    assert bak_rows == [(1, "before")]

    # Live DB must have the new column
    with sqlite3.connect(db_path) as live_conn:
        live_cols = {row[1] for row in live_conn.execute("PRAGMA table_info(legacy_items)")}
        live_rows = live_conn.execute("SELECT id, name, note FROM legacy_items").fetchall()
    assert "note" in live_cols
    assert live_rows == [(1, "before", "v2")]

    # Second run must not re-execute the already applied migration
    reapplied = run_versioned_migrations(engine, migrations=migrations)
    assert reapplied == []
    assert calls == 1


def test_failed_migration_does_not_record_version_and_keeps_backup(tmp_app_data):
    init_db()
    db_path = tmp_app_data / "meta" / "app.db"
    engine = get_engine()

    def _failing_migration(eng) -> None:
        with eng.begin() as conn:
            conn.execute(text("CREATE TABLE should_rollback (id INTEGER PRIMARY KEY)"))
        raise RuntimeError("boom during migration")

    migrations = (
        *VERSIONED_MIGRATIONS,
        ("9902_failing_step", "Broken migration", _failing_migration),
    )

    with pytest.raises(RuntimeError, match="boom during migration"):
        run_versioned_migrations(engine, migrations=migrations)

    assert get_applied_migrations(engine) == _expected_builtin_versions()
    backup_path = db_path.with_name(f"{db_path.name}.pre-migration-9902_failing_step.bak")
    assert backup_path.is_file()


def test_existing_backup_is_not_overwritten_and_empty_db_skips_backup(tmp_path):
    empty_db = tmp_path / "empty.db"
    empty_engine = create_engine(f"sqlite:///{empty_db}")
    with sqlite3.connect(empty_db):
        pass
    assert _backup_db_before_versioned_migration(empty_engine, "0002_any") is None
    assert list(tmp_path.glob("*.bak")) == []
    empty_engine.dispose()

    mem_engine = create_engine("sqlite:///:memory:")
    assert _backup_db_before_versioned_migration(mem_engine, "0002_any") is None
    mem_engine.dispose()

    real_db = tmp_path / "legacy.db"
    with sqlite3.connect(real_db) as conn:
        conn.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO projects (id, name) VALUES (1, 'p1')")
        conn.commit()

    existing_backup = real_db.with_name(f"{real_db.name}.pre-migration-0002_test.bak")
    existing_backup.write_bytes(b"sentinel-backup-bytes")

    real_engine = create_engine(f"sqlite:///{real_db}")
    returned = _backup_db_before_versioned_migration(real_engine, "0002_test")
    assert returned == existing_backup
    assert existing_backup.read_bytes() == b"sentinel-backup-bytes"
    real_engine.dispose()


def test_backup_captures_uncheckpointed_wal_frames_and_cleans_temp_files(tmp_path):
    wal_db = tmp_path / "wal_app.db"
    writer = sqlite3.connect(wal_db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, val TEXT)")
    writer.execute("INSERT INTO items (id, val) VALUES (1, 'in-wal')")
    writer.commit()

    wal_sidecar = tmp_path / "wal_app.db-wal"
    assert wal_sidecar.is_file() and wal_sidecar.stat().st_size > 0

    engine = create_engine(f"sqlite:///{wal_db}")
    try:
        backup_path = _backup_db_before_versioned_migration(engine, "0002_wal_check")
    finally:
        engine.dispose()
        writer.close()

    assert backup_path is not None and backup_path.is_file()
    with sqlite3.connect(backup_path) as bak_conn:
        rows = bak_conn.execute("SELECT id, val FROM items").fetchall()
    assert rows == [(1, "in-wal")]
    assert list(tmp_path.glob(".*.tmp-*")) == []


def test_migration_0005_adds_coverage_metadata_json_to_legacy_checkpoints(tmp_app_data):
    init_db()
    db_path = tmp_app_data / "meta" / "app.db"
    reset_engine()

    with sqlite3.connect(db_path) as conn:
        conn.execute('DROP TABLE "task_test_point_checkpoints"')
        conn.execute(
            """
            CREATE TABLE task_test_point_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                retrieval_checkpoint_id INTEGER,
                attempt INTEGER NOT NULL DEFAULT 1,
                version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending',
                auto_review INTEGER NOT NULL DEFAULT 0,
                decision_hash TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT,
                resume_claim_token TEXT,
                resume_claimed_at DATETIME,
                resume_started_at DATETIME,
                resume_status TEXT NOT NULL DEFAULT 'idle',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO task_test_point_checkpoints (task_id, attempt, status) "
            "VALUES (1, 1, 'confirmed')"
        )
        conn.execute(
            "DELETE FROM schema_migrations WHERE version = '0005_checkpoint_coverage_metadata'"
        )
        conn.commit()

    init_db()

    applied = get_applied_migrations(get_engine())
    assert "0005_checkpoint_coverage_metadata" in applied
    baks = list(db_path.parent.glob("*.pre-migration-*.bak"))
    assert baks, "migrating legacy db should produce a .bak backup"

    with sqlite3.connect(db_path) as conn:
        cols = {
            row[1]
            for row in conn.execute('PRAGMA table_info("task_test_point_checkpoints")').fetchall()
        }
        assert "coverage_metadata_json" in cols
        row = conn.execute(
            "SELECT task_id, status, coverage_metadata_json FROM task_test_point_checkpoints"
        ).fetchone()
        assert row == (1, "confirmed", "{}")


def test_migration_0006_adds_submitter_columns_to_legacy_tables(tmp_app_data):
    init_db()
    db_path = tmp_app_data / "meta" / "app.db"
    reset_engine()

    with sqlite3.connect(db_path) as conn:
        conn.execute('DROP TABLE "generation_tasks"')
        conn.execute(
            """
            CREATE TABLE generation_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requirement_id INTEGER NOT NULL,
                project_id INTEGER,
                wiki_space_id INTEGER,
                status TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute('DROP TABLE "test_cases"')
        conn.execute(
            """
            CREATE TABLE test_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requirement_id INTEGER NOT NULL,
                case_key TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                content_md TEXT NOT NULL DEFAULT '',
                priority TEXT NOT NULL DEFAULT 'P1',
                status TEXT NOT NULL DEFAULT 'active',
                revision INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO generation_tasks (requirement_id, status) VALUES (1, 'draft')"
        )
        conn.execute(
            "INSERT INTO test_cases (requirement_id, case_key, title, content_md) "
            "VALUES (1, 'TC-001', 'Legacy', 'Body')"
        )
        conn.execute(
            "DELETE FROM schema_migrations WHERE version = '0006_submitter_user_columns'"
        )
        conn.commit()

    init_db()

    applied = get_applied_migrations(get_engine())
    assert "0006_submitter_user_columns" in applied
    with sqlite3.connect(db_path) as conn:
        for table in ("generation_tasks", "test_cases"):
            cols = {
                row[1]
                for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            assert "created_by_user_id" in cols
            assert "created_by_username" in cols
        assert conn.execute(
            "SELECT created_by_user_id, created_by_username FROM test_cases"
        ).fetchone() == (None, None)
