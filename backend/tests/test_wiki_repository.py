import os
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app import config
from app.db import get_engine, init_db
from app.models.entities import (
    Document,
    WikiPageRevision,
    WikiPageRow,
    WikiPageSource,
)
from app.services.wiki_repository import (
    WikiPageAlreadyExistsError,
    WikiRepository,
    page_key_lock,
    page_path,
)
from app.services.wiki_schema import WikiFrontmatter, WikiPage
from app.services.wiki_staging import WikiStaging, relative_page_path


def _seed_document(session: Session) -> Document:
    document = Document(
        filename="rules.md",
        stored_path="raw/sources/rules.md",
        content_type="text/markdown",
        sha256="a" * 64,
        status="ready",
    )
    session.add(document)
    session.flush()
    return document


def _rule_page(document_id: int, *, body: str = "余额不足时拒绝下单。") -> WikiPage:
    metadata = WikiFrontmatter(
        page_key="rule.order.insufficient-balance",
        title="余额不足下单规则",
        type="rule",
        domain="spot-order",
        aliases=["可用余额不足"],
        tags=["余额", "下单"],
        sources=[
            {
                "document_id": document_id,
                "chunk_ids": [1, 2],
                "clauses": ["3.5.2"],
            }
        ],
        status="published",
    )
    return WikiPage(frontmatter=metadata, body=body)


def test_page_paths_are_backend_owned_and_reject_traversal(tmp_app_data):
    assert relative_page_path(
        "rule", "rule.order.insufficient-balance"
    ).as_posix() == "rules/rule.order.insufficient-balance.md"
    resolved = page_path("rule", "rule.order.insufficient-balance")
    assert resolved.is_relative_to(config.WIKI_DIR.resolve())

    with pytest.raises(ValueError):
        relative_page_path("business", "rule.order.balance")
    with pytest.raises(ValueError):
        relative_page_path("rule", "../outside")


def test_repository_create_read_list_update_and_archive(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        document = _seed_document(session)
        repository = WikiRepository(session)

        created = repository.create(_rule_page(document.id), reason="initial ingest")
        assert created.revision == 1
        assert created.path.is_file()
        assert created.path == page_path("rule", created.page_key)
        assert repository.read(created.page_key).body == "余额不足时拒绝下单。"
        assert [item.page_key for item in repository.list()] == [created.page_key]

        source_rows = session.exec(
            select(WikiPageSource).where(WikiPageSource.page_id == created.id)
        ).all()
        assert len(source_rows) == 1
        assert source_rows[0].document_id == document.id

        first_hash = created.row.content_hash
        updated = repository.update(
            created.page_key,
            _rule_page(document.id, body="余额不足时拒绝下单，并且不得冻结资金。"),
            reason="clarify funds",
        )
        assert updated.revision == 2
        assert updated.row.content_hash != first_hash
        revisions = session.exec(
            select(WikiPageRevision)
            .where(WikiPageRevision.page_id == updated.id)
            .order_by(WikiPageRevision.revision)
        ).all()
        assert [item.revision for item in revisions] == [1, 2]
        assert [item.operation for item in revisions] == ["create", "update"]
        assert revisions[0].content_md == "余额不足时拒绝下单。"

        archived = repository.archive(updated.page_key, reason="superseded")
        assert archived.status == "archived"
        assert archived.revision == 3
        assert archived.path.is_file()
        assert repository.list(include_archived=False) == []
        assert repository.read(archived.page_key).frontmatter.status == "archived"


def test_duplicate_page_key_is_rejected_without_extra_revision(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        document = _seed_document(session)
        repository = WikiRepository(session)
        created = repository.create(_rule_page(document.id))

        with pytest.raises(WikiPageAlreadyExistsError):
            repository.create(_rule_page(document.id))

        revisions = session.exec(
            select(WikiPageRevision).where(WikiPageRevision.page_id == created.id)
        ).all()
        assert len(revisions) == 1


def test_invalid_candidate_is_cleaned_from_staging(tmp_app_data):
    staging = WikiStaging("invalid-candidate")
    operation_dir = staging.directory
    with pytest.raises(ValueError):
        with staging:
            staging.stage_raw("not frontmatter")
    assert not operation_dir.exists()


def test_replace_failure_rolls_back_new_page_and_cleans_staging(
    tmp_app_data, monkeypatch
):
    init_db()
    with Session(get_engine()) as session:
        document = _seed_document(session)
        repository = WikiRepository(session)
        target = page_path("rule", "rule.order.insufficient-balance")

        def fail_replace(source, destination):
            raise OSError("injected replace failure")

        monkeypatch.setattr(os, "replace", fail_replace)
        with pytest.raises(OSError, match="injected replace failure"):
            repository.create(_rule_page(document.id))

        assert not target.exists()
        assert session.exec(select(WikiPageRow)).all() == []
        assert session.exec(select(WikiPageRevision)).all() == []
        staging_root = config.WIKI_DIR / ".staging"
        assert not staging_root.exists() or not any(staging_root.iterdir())


def test_commit_failure_restores_old_file_and_database(tmp_app_data, monkeypatch):
    init_db()
    with Session(get_engine()) as session:
        document = _seed_document(session)
        repository = WikiRepository(session)
        created = repository.create(_rule_page(document.id))
        old_bytes = created.path.read_bytes()
        old_hash = created.row.content_hash
        original_commit = session.commit

        def fail_commit():
            raise RuntimeError("injected commit failure")

        monkeypatch.setattr(session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected commit failure"):
            repository.update(
                created.page_key,
                _rule_page(document.id, body="不应被提交的新规则。"),
            )
        monkeypatch.setattr(session, "commit", original_commit)

        assert created.path.read_bytes() == old_bytes
        session.expire_all()
        row = session.exec(
            select(WikiPageRow).where(WikiPageRow.page_key == created.page_key)
        ).one()
        assert row.revision == 1
        assert row.content_hash == old_hash
        revisions = session.exec(
            select(WikiPageRevision).where(WikiPageRevision.page_id == row.id)
        ).all()
        assert len(revisions) == 1


def test_page_key_lock_is_reentrant():
    with page_key_lock("rule.order.balance"):
        with page_key_lock("rule.order.balance"):
            pass
