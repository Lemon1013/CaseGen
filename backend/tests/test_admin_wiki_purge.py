from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import get_engine
from app.main import create_app
from app.models.entities import (
    Document,
    GenerationTask,
    IngestJob,
    TaskCitation,
    WikiPageRevision,
    WikiPageRow,
    WikiPageSource,
    WikiReviewItem,
    WikiSpace,
    User,
)
from app.services.auth import hash_password
from app.services.wiki_fts import index_counts, rebuild_fts, search_wiki
from app.services.wiki_repository import page_path
import app.api.wiki as wiki_api


def _origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


def _admin_client(tmp_app_data, monkeypatch) -> TestClient:
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    client = TestClient(create_app())
    response = client.post(
        "/api/auth/setup",
        headers=_origin(),
        json={"username": "admin", "display_name": "Admin", "password": "password1234"},
    )
    assert response.status_code == 200
    return client


def _seed_page(session: Session, space: WikiSpace, *, page_key: str, status: str = "archived", path: str | None = None):
    row = WikiPageRow(
        path=path or page_path("rule", page_key, space_slug=space.slug).relative_to(config.WIKI_DIR).as_posix(),
        title=page_key,
        page_type="rule",
        page_key=page_key,
        status=status,
        space_id=space.id,
        content_hash="hash",
    )
    session.add(row)
    session.flush()
    return row


def _preview(client: TestClient) -> dict:
    response = client.get("/api/admin/wiki/purge/preview", headers=_origin())
    assert response.status_code == 200
    return response.json()


def _execute(client: TestClient, preview: dict, **overrides):
    payload = {
        "scope": "all",
        "plan_hash": preview["plan_hash"],
        "confirmation_text": preview["confirmation_text"],
    }
    payload.update(overrides)
    return client.post("/api/admin/wiki/purge", headers=_origin(), json=payload)


def test_auth_off_and_non_admin_are_rejected(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    assert client.get("/api/admin/wiki/purge/preview").status_code == 503

    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    client = TestClient(create_app())
    setup = client.post("/api/auth/setup", headers=_origin(), json={"username": "admin", "password": "password1234"})
    assert setup.status_code == 200
    with Session(get_engine()) as session:
        session.add(User(username="operator", display_name="Operator", password_hash=hash_password("password1234"), role="user", is_active=True))
        session.commit()
    client.post("/api/auth/logout", headers=_origin())
    assert client.post("/api/auth/login", headers=_origin(), json={"username": "operator", "password": "password1234"}).status_code == 200
    assert client.get("/api/admin/wiki/purge/preview", headers=_origin()).status_code == 403


def test_global_purge_removes_archived_graph_and_files_but_retains_published(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    with Session(get_engine()) as session:
        default = session.exec(select(WikiSpace).where(WikiSpace.slug == "default")).one()
        other = WikiSpace(name="Other", slug="other")
        session.add(other)
        session.flush()
        archived = _seed_page(session, default, page_key="rule.archived")
        retained = _seed_page(session, default, page_key="rule.retained", status="published")
        other_archived = _seed_page(session, other, page_key="rule.other-archived")
        document = Document(filename="source.md", stored_path="raw/source.md", content_type="text/markdown", sha256="a" * 64, status="ready", space_id=default.id)
        session.add(document)
        session.flush()
        session.add(WikiPageSource(page_id=int(archived.id), document_id=int(document.id)))
        session.add(WikiPageRevision(page_id=int(archived.id), revision=1, content_md="old"))
        session.add(WikiReviewItem(page_id=int(archived.id), space_id=default.id, reason="review"))
        task = GenerationTask(requirement_id=1, status="done", wiki_space_id=default.id)
        session.add(task)
        session.flush()
        session.add(TaskCitation(task_id=int(task.id), wiki_page_id=int(archived.id), title="old", path="old.md"))
        session.commit()
        document_id = int(document.id)
        citation_id = int(session.exec(select(TaskCitation)).one().id)
        for row in (archived, retained, other_archived):
            path = Path(config.WIKI_DIR) / row.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {row.page_key}\n", encoding="utf-8")
    preview = _preview(client)
    assert preview["totals"]["pages"] == 2
    assert _execute(client, preview).status_code == 200
    with Session(get_engine()) as session:
        assert session.get(WikiPageRow, archived.id) is None
        assert session.get(WikiPageRow, other_archived.id) is None
        assert session.get(WikiPageRow, retained.id) is not None
        assert session.get(Document, document_id) is not None
        assert session.exec(select(WikiPageSource)).all() == []
        assert session.exec(select(WikiPageRevision)).all() == []
        assert session.exec(select(WikiReviewItem)).all() == []
        assert session.get(TaskCitation, citation_id) is not None
    assert not (Path(config.WIKI_DIR) / archived.path).exists()
    assert (Path(config.WIKI_DIR) / retained.path).exists()


def test_stale_file_child_and_confirmation_are_rejected(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    with Session(get_engine()) as session:
        default = session.exec(select(WikiSpace).where(WikiSpace.slug == "default")).one()
        row = _seed_page(session, default, page_key="rule.stale")
        path = Path(config.WIKI_DIR) / row.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("before", encoding="utf-8")
        session.commit()
    preview = _preview(client)
    assert _execute(client, preview, confirmation_text="wrong").status_code == 409
    path.write_text("changed", encoding="utf-8")
    assert _execute(client, preview).status_code == 409


def test_default_legacy_active_job_blocks_and_missing_file_is_allowed(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    with Session(get_engine()) as session:
        default = session.exec(select(WikiSpace).where(WikiSpace.slug == "default")).one()
        row = _seed_page(session, default, page_key="rule.missing")
        session.add(IngestJob(document_id=999, space_id=None, status="running"))
        session.commit()
    preview = _preview(client)
    assert preview["active_jobs"]
    assert _execute(client, preview).status_code == 409
    with Session(get_engine()) as session:
        session.exec(select(IngestJob)).first().status = "done"
        session.commit()
    preview = _preview(client)
    assert preview["missing"]
    assert _execute(client, preview).status_code == 200


def test_symlink_and_traversal_are_blocked_without_deleting_target(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    outside = Path(tmp_app_data).parent / "outside.md"
    outside.write_text("do not delete", encoding="utf-8")
    with Session(get_engine()) as session:
        default = session.exec(select(WikiSpace).where(WikiSpace.slug == "default")).one()
        row = _seed_page(session, default, page_key="rule.symlink", path="pages/link.md")
        link = Path(config.WIKI_DIR) / row.path
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)
        session.commit()
    preview = _preview(client)
    assert preview["unsafe"]
    assert _execute(client, preview).status_code == 422
    assert outside.exists()


@pytest.mark.parametrize("failure", ["unlink", "commit"])
def test_file_or_commit_failure_restores_database_and_file(tmp_app_data, monkeypatch, failure):
    client = _admin_client(tmp_app_data, monkeypatch)
    with Session(get_engine()) as session:
        default = session.exec(select(WikiSpace).where(WikiSpace.slug == "default")).one()
        row = _seed_page(session, default, page_key=f"rule.failure-{failure}")
        path = Path(config.WIKI_DIR) / row.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("recover me", encoding="utf-8")
        session.commit()
        row_id = int(row.id)
    preview = _preview(client)
    if failure == "unlink":
        monkeypatch.setattr(os, "unlink", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected unlink failure")))
    else:
        from sqlmodel import Session as SQLModelSession
        original = SQLModelSession.commit
        monkeypatch.setattr(SQLModelSession, "commit", lambda _self: (_ for _ in ()).throw(RuntimeError("injected commit failure")))
    response = _execute(client, preview)
    assert response.status_code == 500
    assert path.read_text(encoding="utf-8") == "recover me"
    with Session(get_engine()) as session:
        assert session.get(WikiPageRow, row_id) is not None


def test_empty_plan_is_idempotent_and_derived_indexes_are_clean(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    preview = _preview(client)
    result = _execute(client, preview)
    assert result.status_code == 200
    assert result.json()["status"] == "completed_already_purged"
    assert index_counts(get_engine())["wiki_pages"] == 0
    assert not search_wiki(get_engine(), "archived")


def test_static_parent_symlink_is_unsafe_and_external_file_survives(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    outside = Path(tmp_app_data).parent / "outside-pages"
    outside.mkdir()
    external = outside / "rule.static.md"
    external.write_text("do not touch", encoding="utf-8")
    with Session(get_engine()) as session:
        default = session.exec(select(WikiSpace).where(WikiSpace.slug == "default")).one()
        row = _seed_page(session, default, page_key="rule.static-parent")
        canonical_parent = (Path(config.WIKI_DIR) / row.path).parent
        canonical_parent.parent.mkdir(parents=True, exist_ok=True)
        canonical_parent.symlink_to(outside, target_is_directory=True)
        session.commit()
    preview = _preview(client)
    assert preview["unsafe"]
    assert _execute(client, preview).status_code == 422
    assert external.read_text(encoding="utf-8") == "do not touch"


def test_parent_symlink_replaced_after_locked_preview_is_rejected(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    outside = Path(tmp_app_data).parent / "outside-race"
    outside.mkdir()
    external = outside / "rule.race.md"
    external.write_text("do not delete", encoding="utf-8")
    with Session(get_engine()) as session:
        default = session.exec(select(WikiSpace).where(WikiSpace.slug == "default")).one()
        row = _seed_page(session, default, page_key="rule.parent-race")
        path = Path(config.WIKI_DIR) / row.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("inside", encoding="utf-8")
        session.commit()
    preview = _preview(client)
    original_backup = wiki_api._backup_file
    swapped = False

    def swap_parent_then_backup(path, backup_root):
        nonlocal swapped
        if not swapped:
            parent = path.parent
            moved = parent.with_name(parent.name + ".moved")
            parent.rename(moved)
            parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_backup(path, backup_root)

    monkeypatch.setattr(wiki_api, "_backup_file", swap_parent_then_backup)
    response = _execute(client, preview)
    assert response.status_code in {409, 422}
    assert external.read_text(encoding="utf-8") == "do not delete"
    assert (path.parent.with_name(path.parent.name + ".moved") / path.name).read_text(encoding="utf-8") == "inside"


@pytest.mark.parametrize("legacy_name", ["index.md", "overview.md"])
def test_space_level_legacy_paths_are_unsafe_and_preserved(tmp_app_data, monkeypatch, legacy_name):
    client = _admin_client(tmp_app_data, monkeypatch)
    with Session(get_engine()) as session:
        default = session.exec(select(WikiSpace).where(WikiSpace.slug == "default")).one()
        space_slug = str(default.slug)
        row = WikiPageRow(
            path=f"spaces/{space_slug}/{legacy_name}",
            title="legacy space artifact",
            page_type="rule",
            page_key="rule.legacy-space-artifact",
            status="archived",
            space_id=default.id,
            content_hash="hash",
        )
        session.add(row)
        session.commit()
    artifact = Path(config.WIKI_DIR) / f"spaces/{space_slug}/{legacy_name}"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("keep this artifact", encoding="utf-8")
    preview = _preview(client)
    assert preview["unsafe"]
    assert _execute(client, preview).status_code == 422
    assert artifact.read_text(encoding="utf-8") == "keep this artifact"


def test_child_content_change_with_same_id_stales_purge_plan(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    with Session(get_engine()) as session:
        default = session.exec(select(WikiSpace).where(WikiSpace.slug == "default")).one()
        row = _seed_page(session, default, page_key="rule.child-content")
        path = Path(config.WIKI_DIR) / row.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content", encoding="utf-8")
        revision = WikiPageRevision(page_id=int(row.id), revision=1, content_md="before")
        session.add(revision)
        session.commit()
        revision_id = int(revision.id)
    preview = _preview(client)
    with Session(get_engine()) as session:
        changed = session.get(WikiPageRevision, revision_id)
        assert changed is not None
        changed.content_md = "after"
        session.add(changed)
        session.commit()
    assert _execute(client, preview).status_code == 409


def test_derived_rebuild_warning_keeps_purge_canonical_and_allows_recovery(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    with Session(get_engine()) as session:
        default = session.exec(select(WikiSpace).where(WikiSpace.slug == "default")).one()
        row = _seed_page(session, default, page_key="rule.derived-warning")
        canonical = Path(config.WIKI_DIR) / row.path
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text("# derived warning", encoding="utf-8")
        session.commit()
    preview = _preview(client)
    monkeypatch.setattr(wiki_api, "rebuild_overview", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected overview failure")))
    response = _execute(client, preview)
    assert response.status_code == 200
    assert response.json()["status"] == "completed_with_warnings"
    assert not canonical.exists()
    index_response = client.get("/api/wiki/index", headers=_origin())
    assert index_response.status_code == 200
    with Session(get_engine()) as session:
        rebuild_fts(session, space_id=1)
        session.commit()
        assert index_counts(session)["wiki_pages"] == 0
