"""Tests for lightweight versioned SQLite schema migrations."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, text

from app.db import (
    _backup_db_before_versioned_migration,
    get_applied_migrations,
    get_engine,
    init_db,
    reset_engine,
    run_versioned_migrations,
)


def test_fresh_init_db_records_baseline_without_backup(tmp_app_data):
    db_path = tmp_app_data / "meta" / "app.db"
    assert not db_path.exists()

    init_db()

    assert get_applied_migrations(get_engine()) == ["0001_v2_refactor_baseline"]
    assert list(db_path.parent.glob("*.pre-migration-*.bak")) == []


def test_repeated_init_db_is_idempotent(tmp_app_data):
    init_db()
    db_path = tmp_app_data / "meta" / "app.db"

    reset_engine()
    init_db()
    reset_engine()
    init_db()

    assert get_applied_migrations(get_engine()) == ["0001_v2_refactor_baseline"]
    assert list(db_path.parent.glob("*.pre-migration-*.bak")) == []
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count == 1


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
        ("0001_v2_refactor_baseline", "Record v2 refactor baseline", lambda _e: None),
        ("0002_test_add_col", "Add note column to legacy_items", _add_col_migration),
    )

    applied = run_versioned_migrations(engine, migrations=migrations)
    assert applied == ["0002_test_add_col"]
    assert calls == 1
    assert get_applied_migrations(engine) == [
        "0001_v2_refactor_baseline",
        "0002_test_add_col",
    ]

    backup_path = db_path.with_name(f"{db_path.name}.pre-migration-0002_test_add_col.bak")
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
        ("0001_v2_refactor_baseline", "Record v2 refactor baseline", lambda _e: None),
        ("0003_failing_step", "Broken migration", _failing_migration),
    )

    with pytest.raises(RuntimeError, match="boom during migration"):
        run_versioned_migrations(engine, migrations=migrations)

    assert get_applied_migrations(engine) == ["0001_v2_refactor_baseline"]
    backup_path = db_path.with_name(f"{db_path.name}.pre-migration-0003_failing_step.bak")
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
