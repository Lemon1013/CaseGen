import sqlite3
import shutil

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app.db import get_engine, init_db
from app.db import reset_engine
from app.models import entities
from app.models.entities import ModelConfig, Requirement


def _add_legacy_case_log_columns_and_row(
    db_path, case_id: int, sentinel: str
) -> None:
    """Seed one pre-release reversible audit row for migration coverage."""

    with sqlite3.connect(db_path) as conn:
        existing = {
            str(row[1])
            for row in conn.execute(
                'PRAGMA table_info("test_case_operation_logs")'
            ).fetchall()
        }
        for name in (
            "before_content_md",
            "after_content_md",
            "diff_text",
            "diff_json",
        ):
            if name not in existing:
                conn.execute(
                    f'ALTER TABLE test_case_operation_logs ADD COLUMN "{name}" TEXT'
                )
        conn.execute(
            """
            INSERT INTO test_case_operation_logs
              (test_case_id, operation, changed_fields_json, before_hash, after_hash,
               before_length, after_length, added_lines, deleted_lines, title_changed,
               diff_summary, before_content_md, after_content_md, diff_text, diff_json, created_at)
            VALUES (?, 'edit', '["content_md"]', 'before-hash', 'after-hash',
                    12, 14, 1, 1, 0, '正文已更新', ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                case_id,
                f"{sentinel}-before",
                f"{sentinel}-after",
                f"-{sentinel}-before\\n+{sentinel}-after",
                f'{{"before":"{sentinel}-before"}}',
            ),
        )
        conn.commit()


def _assert_legacy_log_is_sanitized(db_path, case_id: int, sentinel: str) -> None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT before_content_md, after_content_md, diff_text, diff_json,
                   operation, changed_fields_json, before_hash, after_hash,
                   before_length, after_length, added_lines, deleted_lines,
                   title_changed, diff_summary, created_at
            FROM test_case_operation_logs
            WHERE test_case_id = ?
            """,
            (case_id,),
        ).fetchone()
    assert row is not None
    assert row[:4] == (None, None, "", "{}")
    assert row[4:14] == (
        "edit",
        '["content_md"]',
        "before-hash",
        "after-hash",
        12,
        14,
        1,
        1,
        0,
        "正文已更新",
    )
    assert row[14] is not None
    assert sentinel.encode("utf-8") not in db_path.read_bytes()


def test_init_db_creates_sqlite_file(tmp_app_data):
    init_db()
    db_path = tmp_app_data / "meta" / "app.db"
    assert db_path.exists()


def test_init_db_creates_expected_tables(tmp_app_data):
    init_db()
    engine = get_engine()
    table_names = set(engine.dialect.get_table_names(engine.connect()))
    expected = {
        "models",
        "prompt_templates",
        "documents",
        "ingest_jobs",
        "wiki_pages",
        "requirements",
        "generation_tasks",
        "task_citations",
        "case_drafts",
        "review_results",
        "prompt_revisions",
        "task_events",
    }
    assert expected.issubset(table_names)


def test_model_config_roundtrip(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        row = ModelConfig(
            name="local",
            base_url="http://localhost:11434/v1",
            api_key="sk-test",
            model_name="gpt-test",
            is_default=True,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        assert row.id is not None

        fetched = session.exec(select(ModelConfig).where(ModelConfig.id == row.id)).one()
        assert fetched.name == "local"
        assert fetched.is_default is True
        assert fetched.created_at is not None
        assert fetched.updated_at is not None


def test_case_log_migration_clears_legacy_reversible_body_columns(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        requirement = Requirement(title="需求", description="说明")
        session.add(requirement)
        session.flush()
        case = entities.TestCase(
            requirement_id=requirement.id,
            case_key="TC-001",
            title="登录",
            content_md="当前正文",
        )
        session.add(case)
        session.commit()
        case_id = int(case.id)

    db_path = tmp_app_data / "meta" / "app.db"
    _add_legacy_case_log_columns_and_row(db_path, case_id, "旧正文")

    reset_engine()
    init_db()

    backup_path = db_path.with_name(db_path.name + ".case-management-pre-migration.bak")
    assert backup_path.exists()
    _assert_legacy_log_is_sanitized(db_path, case_id, "旧正文")
    _assert_legacy_log_is_sanitized(backup_path, case_id, "旧正文")


def test_case_migration_preflights_duplicate_keys_and_backs_up(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        requirement = Requirement(title="需求", description="说明")
        session.add(requirement)
        session.commit()
        requirement_id = int(requirement.id)

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text('DROP INDEX IF EXISTS "uq_test_cases_source_identity"'))
        conn.execute(text('DROP INDEX IF EXISTS "uq_test_cases_requirement_case_key_normalized"'))

    with Session(engine) as session:
        cases = []
        for index in range(2):
            case = entities.TestCase(
                requirement_id=requirement_id,
                case_key="TC-001",
                title=f"重复 {index}",
                content_md="正文",
                source_task_id=11,
                source_draft_id=21,
                source_case_key="TC-001",
            )
            session.add(case)
            cases.append(case)
        session.commit()
        case_id = int(cases[0].id)
        case_ids = [int(case.id) for case in cases]

    reset_engine()
    db_path = tmp_app_data / "meta" / "app.db"
    sentinel = "duplicate-reversible-body"
    _add_legacy_case_log_columns_and_row(db_path, case_id, sentinel)
    existing_backup = db_path.with_name(db_path.name + ".case-management-pre-migration.bak")
    shutil.copyfile(db_path, existing_backup)
    with pytest.raises(RuntimeError, match="duplicate test cases") as exc_info:
        init_db()

    message = str(exc_info.value)
    assert "source identity duplicates" in message
    assert "requirement/case_key duplicates" in message
    backup_path = db_path.with_name(db_path.name + ".case-management-pre-migration.bak")
    assert backup_path.exists()
    _assert_legacy_log_is_sanitized(db_path, case_id, sentinel)
    _assert_legacy_log_is_sanitized(backup_path, case_id, sentinel)
    with sqlite3.connect(db_path) as conn:
        duplicate_rows = conn.execute(
            """
            SELECT id, title, content_md
            FROM test_cases
            WHERE requirement_id = ? AND case_key = 'TC-001'
            ORDER BY id
            """,
            (requirement_id,),
        ).fetchall()
    assert duplicate_rows == [
        (case_ids[0], "重复 0", "正文"),
        (case_ids[1], "重复 1", "正文"),
    ]


@pytest.mark.parametrize("legacy_case_key", ["", "  \t  "])
def test_case_migration_blocks_empty_or_blank_legacy_case_key_before_ddl(
    tmp_app_data, legacy_case_key
):
    init_db()
    with Session(get_engine()) as session:
        requirement = Requirement(title="需求", description="说明")
        session.add(requirement)
        session.commit()
        requirement_id = int(requirement.id)

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text('DROP INDEX IF EXISTS "uq_test_cases_source_identity"'))
        conn.execute(
            text('DROP INDEX IF EXISTS "uq_test_cases_requirement_case_key_normalized"')
        )

    with Session(engine) as session:
        case = entities.TestCase(
            requirement_id=requirement_id,
            case_key=legacy_case_key,
            title="旧用例",
            content_md="当前正文仍应保留",
        )
        session.add(case)
        session.commit()
        case_id = int(case.id)

    reset_engine()
    db_path = tmp_app_data / "meta" / "app.db"
    sentinel = "invalid-key-reversible-body"
    _add_legacy_case_log_columns_and_row(db_path, case_id, sentinel)
    backup_path = db_path.with_name(db_path.name + ".case-management-pre-migration.bak")
    # An existing backup must not skip preflight, and it must be sanitized too.
    shutil.copyfile(db_path, backup_path)

    with pytest.raises(RuntimeError, match="invalid legacy case_key") as exc_info:
        init_db()

    assert "invalid legacy case_key rows" in str(exc_info.value)
    _assert_legacy_log_is_sanitized(db_path, case_id, sentinel)
    _assert_legacy_log_is_sanitized(backup_path, case_id, sentinel)
    with sqlite3.connect(db_path) as conn:
        stored_case_key = conn.execute(
            "SELECT case_key FROM test_cases WHERE id = ?", (case_id,)
        ).fetchone()[0]
        index_names = {
            row[1] for row in conn.execute('PRAGMA index_list("test_cases")').fetchall()
        }
    assert stored_case_key == legacy_case_key
    assert "uq_test_cases_source_identity" not in index_names
    assert "uq_test_cases_requirement_case_key_normalized" not in index_names
