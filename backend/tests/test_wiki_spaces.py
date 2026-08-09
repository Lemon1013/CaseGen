import json

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_engine, init_db
from app.main import create_app
from app.models.entities import (
    Document,
    IngestJob,
    SourceChunk,
    WikiPageRow,
    WikiReviewItem,
    WikiSpace,
)
from app.services.hybrid_retrieve import hybrid_retrieve
from app.services.source_chunks_store import replace_chunks_for_document
from app.services.wiki_repository import WikiRepository
from app.services.wiki_schema import WikiFrontmatter, WikiPage
from app.services.wiki_spaces import get_default_space, space_root


def _document(session: Session, *, space_id: int, filename: str) -> Document:
    row = Document(
        filename=filename,
        stored_path=f"raw/sources/{filename}",
        content_type="text/markdown",
        sha256=(str(space_id) * 64)[:64],
        status="ready",
        space_id=space_id,
    )
    session.add(row)
    session.flush()
    return row


def _page(document_id: int, body: str) -> WikiPage:
    return WikiPage(
        frontmatter=WikiFrontmatter(
            page_key="rule.shared-key",
            title="隔离检索规则",
            type="rule",
            domain="isolation",
            sources=[{"document_id": document_id}],
            status="published",
        ),
        body=body,
    )


def test_space_crud_document_and_task_selection(tmp_app_data):
    client = TestClient(create_app())

    listed = client.get("/api/wiki-spaces")
    assert listed.status_code == 200
    default = next(space for space in listed.json() if space["slug"] == "default")

    created = client.post(
        "/api/wiki-spaces",
        json={"name": "项目甲", "slug": "project-a", "description": "甲项目知识库"},
    )
    assert created.status_code == 201
    project = created.json()

    uploaded = client.post(
        "/api/documents",
        data={"space_id": str(project["id"])},
        files={"file": ("rules.md", "项目甲专属规则", "text/markdown")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["space_id"] == project["id"]
    assert client.get("/api/documents", params={"space_id": project["id"]}).json()[0]["id"] == uploaded.json()["id"]
    assert client.get("/api/documents", params={"space_id": default["id"]}).json() == []

    task = client.post(
        "/api/tasks",
        json={
            "title": "项目甲用例",
            "description": "仅使用项目甲知识",
            "wiki_space_id": project["id"],
        },
    )
    assert task.status_code == 200
    assert task.json()["wiki_space_id"] == project["id"]
    assert task.json()["wiki_space_name"] == "项目甲"

    archived = client.post(f"/api/wiki-spaces/{project['id']}/archive")
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    rejected = client.post(
        "/api/tasks",
        json={"title": "不可创建", "description": "归档空间", "wiki_space_id": project["id"]},
    )
    assert rejected.status_code == 422
    assert client.post(f"/api/wiki-spaces/{default['id']}/archive").status_code == 409


def test_same_page_key_and_retrieval_are_strictly_space_scoped(tmp_app_data):
    init_db()
    with Session(get_engine()) as session:
        default = get_default_space(session)
        assert default is not None
        project = WikiSpace(name="项目乙", slug="project-b", description="", status="active")
        session.add(project)
        session.flush()
        assert default.id is not None and project.id is not None

        default_doc = _document(session, space_id=default.id, filename="default.md")
        project_doc = _document(session, space_id=project.id, filename="project.md")
        default_chunks = replace_chunks_for_document(
            session,
            default_doc.id,
            "9.9.1 星河校验词：默认空间只允许蓝色方案。",
            space_id=default.id,
        )
        project_chunks = replace_chunks_for_document(
            session,
            project_doc.id,
            "9.9.1 星河校验词：项目乙只允许金色方案。",
            space_id=project.id,
        )
        default_page = WikiRepository(session, space_id=default.id).create(
            _page(default_doc.id, "星河校验词采用蓝色方案。")
        )
        project_page = WikiRepository(session, space_id=project.id).create(
            _page(project_doc.id, "星河校验词采用金色方案。")
        )
        session.commit()

        assert default_page.page_key == project_page.page_key
        assert default_page.path != project_page.path
        assert default_page.path.is_relative_to(space_root(default))
        assert project_page.path.is_relative_to(space_root(project))

        default_result = hybrid_retrieve(session, "星河校验词 9.9.1", space_id=default.id)
        project_result = hybrid_retrieve(session, "星河校验词 9.9.1", space_id=project.id)

        assert {hit["space_id"] for hit in default_result["hits"]} == {default.id}
        assert {hit["space_id"] for hit in project_result["hits"]} == {project.id}
        assert default_chunks[0].id in {hit["id"] for hit in default_result["source_hits"]}
        assert project_chunks[0].id not in {hit["id"] for hit in default_result["source_hits"]}
        assert project_chunks[0].id in {hit["id"] for hit in project_result["source_hits"]}
        assert "金色" not in json.dumps(default_result, ensure_ascii=False)
        assert "蓝色" not in json.dumps(project_result, ensure_ascii=False)


def test_legacy_null_rows_are_visible_only_in_default_space(tmp_app_data):
    client = TestClient(create_app())
    with Session(get_engine()) as session:
        default = get_default_space(session)
        project = WikiSpace(name="项目丙", slug="project-c", description="", status="active")
        session.add(project)
        session.flush()
        page = WikiPageRow(
            path="rules/legacy.md",
            title="旧页面",
            page_type="rule",
            page_key="rule.legacy-null",
            space_id=None,
        )
        review = WikiReviewItem(kind="needs_review", reason="旧审核", space_id=None)
        chunk = SourceChunk(document_id=999, text="旧原文", space_id=None)
        project_doc = _document(session, space_id=project.id, filename="project-c.md")
        project_job = IngestJob(
            document_id=project_doc.id,
            space_id=project.id,
            status="failed",
            stage="failed",
        )
        session.add(page)
        session.add(review)
        session.add(chunk)
        session.add(project_job)
        session.commit()
        assert default is not None and default.id is not None and project.id is not None
        default_id, project_id = default.id, project.id
        page_id, review_id, chunk_id = page.id, review.id, chunk.id
        project_doc_id, project_job_id = project_doc.id, project_job.id

    assert client.get("/api/wiki/pages", params={"space_id": project_id}).json() == []
    assert client.get("/api/wiki/reviews", params={"space_id": project_id}).json() == []
    assert client.get(f"/api/wiki/pages/{page_id}", params={"space_id": project_id}).status_code == 404
    assert client.get(f"/api/wiki/reviews/{review_id}", params={"space_id": project_id}).status_code == 404
    assert client.get(f"/api/source-chunks/{chunk_id}", params={"space_id": project_id}).status_code == 404
    assert client.get(f"/api/documents/{project_doc_id}").status_code == 404
    assert client.get(f"/api/documents/{project_doc_id}", params={"space_id": project_id}).status_code == 200
    assert client.get(f"/api/ingest-jobs/{project_job_id}").status_code == 404
    assert client.get(f"/api/ingest-jobs/{project_job_id}", params={"space_id": project_id}).status_code == 200

    assert len(client.get("/api/wiki/pages", params={"space_id": default_id}).json()) == 1
    assert len(client.get("/api/wiki/reviews", params={"space_id": default_id}).json()) == 1
    assert client.get(f"/api/source-chunks/{chunk_id}", params={"space_id": default_id}).status_code == 200
