from sqlmodel import Session, select

from app.db import get_engine, init_db
from app.models.entities import ModelConfig


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
