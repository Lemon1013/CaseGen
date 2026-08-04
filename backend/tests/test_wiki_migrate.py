import json
import sqlite3
from datetime import datetime, timezone

from sqlalchemy import text
from sqlmodel import Session, select

from app import config
from app.db import get_engine, init_db
from app.models.entities import (
    Document,
    IngestJob,
    WikiPageRevision,
    WikiPageRow,
    WikiPageSource,
    WikiReviewItem,
)


def _sqlite_columns(table_name: str) -> set[str]:
    with get_engine().connect() as conn:
        rows = conn.execute(text(f'PRAGMA table_info("{table_name}")')).fetchall()
    return {str(row[1]) for row in rows}


def _create_legacy_database(db_path, *, page_id: int = 7) -> None:
    """Create only the pre-Wiki-2.0 tables needed by the migration test."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY,
                filename VARCHAR NOT NULL,
                stored_path VARCHAR NOT NULL,
                content_type VARCHAR NOT NULL,
                sha256 VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                char_count INTEGER NOT NULL DEFAULT 0,
                error_message VARCHAR,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            CREATE TABLE ingest_jobs (
                id INTEGER PRIMARY KEY,
                document_id INTEGER NOT NULL,
                status VARCHAR NOT NULL,
                step_log_json VARCHAR NOT NULL DEFAULT '[]',
                error_message VARCHAR,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            CREATE TABLE wiki_pages (
                id INTEGER PRIMARY KEY,
                path VARCHAR NOT NULL,
                title VARCHAR NOT NULL,
                page_type VARCHAR NOT NULL,
                source_document_id INTEGER,
                tags_json VARCHAR NOT NULL DEFAULT '[]',
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            """
        )
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        conn.execute(
            """
            INSERT INTO wiki_pages
                (id, path, title, page_type, source_document_id, tags_json,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                page_id,
                "pages/legacy-page.md",
                "旧页面标题",
                "business",
                None,
                json.dumps(["旧标签"], ensure_ascii=False),
                now,
                now,
            ),
        )
        conn.commit()


def test_init_db_creates_wiki2_columns_tables_and_indexes(tmp_app_data):
    init_db()

    assert {
        "page_key",
        "domain",
        "status",
        "revision",
        "aliases_json",
        "content_hash",
    }.issubset(_sqlite_columns("wiki_pages"))
    assert {
        "stage",
        "progress",
        "plan_json",
        "model_ref",
        "prompt_version_ref",
        "cancel_requested",
    }.issubset(_sqlite_columns("ingest_jobs"))

    with get_engine().connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).fetchall()
        }
        page_indexes = {
            row[1]
            for row in conn.execute(text('PRAGMA index_list("wiki_pages")')).fetchall()
        }
        job_indexes = {
            row[1]
            for row in conn.execute(text('PRAGMA index_list("ingest_jobs")')).fetchall()
        }

    assert {
        "wiki_page_sources",
        "wiki_page_revisions",
        "wiki_review_items",
    }.issubset(tables)
    assert "uq_wiki_pages_page_key_not_null" in page_indexes
    assert {"ix_ingest_jobs_status", "ix_ingest_jobs_stage"}.issubset(job_indexes)


def test_old_schema_migrates_without_moving_page_or_losing_identity(tmp_app_data):
    db_path = tmp_app_data / "meta" / "app.db"
    _create_legacy_database(db_path, page_id=7)

    init_db()

    with Session(get_engine()) as session:
        row = session.get(WikiPageRow, 7)
        assert row is not None
        assert row.id == 7
        assert row.path == "pages/legacy-page.md"
        assert row.title == "旧页面标题"
        assert row.page_key == "legacy.page.7"
        assert row.status == "published"
        assert row.revision == 1

    backup_path = config.DB_PATH.with_name(
        config.DB_PATH.name + ".wiki2-pre-migration.bak"
    )
    assert backup_path.exists()
    assert backup_path.read_bytes()


def test_wiki_migration_is_idempotent_and_backup_is_not_replaced(tmp_app_data):
    db_path = tmp_app_data / "meta" / "app.db"
    _create_legacy_database(db_path, page_id=11)

    init_db()
    backup_path = config.DB_PATH.with_name(
        config.DB_PATH.name + ".wiki2-pre-migration.bak"
    )
    backup_bytes = backup_path.read_bytes()

    init_db()

    with Session(get_engine()) as session:
        row = session.get(WikiPageRow, 11)
        assert row is not None
        assert row.page_key == "legacy.page.11"
    assert list(backup_path.parent.glob("app.db.wiki2-pre-migration.bak")) == [
        backup_path
    ]
    assert backup_path.read_bytes() == backup_bytes


def test_partial_column_migration_still_backs_up_before_creating_tables(tmp_app_data):
    db_path = tmp_app_data / "meta" / "app.db"
    _create_legacy_database(db_path, page_id=13)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            ALTER TABLE wiki_pages ADD COLUMN page_key VARCHAR;
            ALTER TABLE wiki_pages ADD COLUMN domain VARCHAR;
            ALTER TABLE wiki_pages ADD COLUMN status VARCHAR DEFAULT 'published';
            ALTER TABLE wiki_pages ADD COLUMN revision INTEGER DEFAULT 1;
            ALTER TABLE wiki_pages ADD COLUMN aliases_json TEXT DEFAULT '[]';
            ALTER TABLE wiki_pages ADD COLUMN content_hash VARCHAR;
            UPDATE wiki_pages SET page_key = 'legacy.page.13';
            CREATE UNIQUE INDEX uq_wiki_pages_page_key_not_null
                ON wiki_pages(page_key) WHERE page_key IS NOT NULL;
            ALTER TABLE ingest_jobs ADD COLUMN stage VARCHAR DEFAULT 'queued';
            ALTER TABLE ingest_jobs ADD COLUMN progress INTEGER DEFAULT 0;
            ALTER TABLE ingest_jobs ADD COLUMN plan_json TEXT DEFAULT '{}';
            ALTER TABLE ingest_jobs ADD COLUMN model_ref VARCHAR;
            ALTER TABLE ingest_jobs ADD COLUMN prompt_version_ref VARCHAR;
            ALTER TABLE ingest_jobs ADD COLUMN cancel_requested BOOLEAN DEFAULT 0;
            """
        )

    init_db()

    backup_path = config.DB_PATH.with_name(
        config.DB_PATH.name + ".wiki2-pre-migration.bak"
    )
    assert backup_path.exists()
    with sqlite3.connect(backup_path) as backup:
        tables = {
            row[0]
            for row in backup.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "wiki_page_sources" not in tables


def test_wiki2_entities_can_be_written_and_read(tmp_app_data):
    init_db()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    with Session(get_engine()) as session:
        document = Document(
            filename="rules.md",
            stored_path="raw/sources/rules.md",
            content_type="text/markdown",
            sha256="a" * 64,
            status="ready",
            created_at=now,
            updated_at=now,
        )
        session.add(document)
        session.flush()

        page = WikiPageRow(
            path="pages/rule.md",
            title="余额规则",
            page_type="rule",
            page_key="rule.order.balance",
            domain="spot-order",
            status="published",
            revision=2,
            aliases_json='["余额不足"]',
            content_hash="b" * 64,
        )
        session.add(page)
        session.flush()

        job = IngestJob(
            document_id=document.id,
            status="queued",
            stage="planning",
            progress=40,
            plan_json='{"page_operations": []}',
            model_ref="model-1",
            prompt_version_ref="wiki-analyze:v2",
        )
        session.add(job)
        session.flush()

        source = WikiPageSource(
            page_id=page.id,
            document_id=document.id,
            chunk_ids_json="[1, 2]",
            clauses_json='["3.5.2"]',
        )
        revision = WikiPageRevision(
            page_id=page.id,
            revision=2,
            frontmatter_json='{"page_key": "rule.order.balance"}',
            content_md="# 余额规则",
            operation="update",
            job_id=job.id,
            reason="补充资金检查",
        )
        review = WikiReviewItem(
            page_id=page.id,
            job_id=job.id,
            kind="conflict",
            status="pending",
            reason="新旧规则数值不一致",
            candidate_frontmatter_json='{"status": "draft"}',
            candidate_content_md="# 待审核规则",
        )
        session.add(source)
        session.add(revision)
        session.add(review)
        session.commit()

        fetched_source = session.exec(
            select(WikiPageSource).where(WikiPageSource.page_id == page.id)
        ).one()
        fetched_revision = session.exec(
            select(WikiPageRevision).where(WikiPageRevision.page_id == page.id)
        ).one()
        fetched_review = session.exec(
            select(WikiReviewItem).where(WikiReviewItem.page_id == page.id)
        ).one()

        assert fetched_source.document_id == document.id
        assert fetched_source.chunk_ids_json == "[1, 2]"
        assert fetched_revision.content_md == "# 余额规则"
        assert fetched_revision.job_id == job.id
        assert fetched_review.status == "pending"
        assert fetched_review.kind == "conflict"
