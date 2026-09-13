"""Requirement document import: parse for the workbench form, archive only.

The imported file is archived under raw/sources/ like document uploads, but it
must never create a Document row or trigger Wiki ingestion: a requirement
document is user input, not knowledge.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import get_engine
from app.main import create_app
from app.models.entities import Document, Requirement, WikiSpace


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


def _default_space(session: Session) -> WikiSpace:
    return session.exec(select(WikiSpace).where(WikiSpace.slug == "default")).one()


def _import_url() -> str:
    with Session(get_engine()) as session:
        project_id = int(_default_space(session).project_id)
    return f"/api/requirements/import-doc?project_id={project_id}"


def test_import_doc_parses_text_and_archives_without_document_row(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    content = "# 登录需求\n用户输入正确密码后可以登录系统首页".encode("utf-8")
    response = client.post(
        _import_url(),
        headers=_origin(),
        files={"file": ("login-requirements.md", content, "text/markdown")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "login-requirements"
    assert "登录系统首页" in body["text"]
    assert body["char_count"] == len(body["text"])
    assert body["stored_path"].startswith("raw/sources/")
    assert body["sha256"] == hashlib.sha256(content).hexdigest()

    stored = Path(config.DATA_DIR) / body["stored_path"]
    assert stored.is_file()
    assert stored.read_bytes() == content

    with Session(get_engine()) as session:
        # Archived on disk only: no Document row, no Wiki ingestion trigger.
        assert session.exec(select(Document)).all() == []


def test_import_doc_rejects_unsupported_extension(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    response = client.post(
        _import_url(),
        headers=_origin(),
        files={"file": ("payload.exe", b"not-a-doc", "application/octet-stream")},
    )
    assert response.status_code == 400
    assert "Unsupported file extension" in response.json()["detail"]


def test_import_doc_rejects_oversize_file(tmp_app_data, monkeypatch):
    # Shrinking the limit keeps the size test fast; the endpoint reads the
    # limit from the config module at request time.
    client = _admin_client(tmp_app_data, monkeypatch)
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 10)
    response = client.post(
        _import_url(),
        headers=_origin(),
        files={"file": ("big.md", b"x" * 64, "text/markdown")},
    )
    assert response.status_code == 400
    assert "File exceeds max size" in response.json()["detail"]


def test_import_doc_rejects_blank_text_file(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    response = client.post(
        _import_url(),
        headers=_origin(),
        files={"file": ("blank.md", b"   \n\t\n", "text/markdown")},
    )
    assert response.status_code == 422
    assert "no extractable text" in response.json()["detail"]


def test_import_doc_rejects_empty_docx(tmp_app_data, monkeypatch, tmp_path):
    from docx import Document as DocxDocument

    client = _admin_client(tmp_app_data, monkeypatch)
    docx_path = tmp_path / "empty.docx"
    DocxDocument().save(docx_path)
    response = client.post(
        _import_url(),
        headers=_origin(),
        files={
            "file": (
                "empty.docx",
                docx_path.read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert response.status_code == 422
    assert "no extractable text" in response.json()["detail"]


def test_import_doc_requires_login_and_same_origin(tmp_app_data, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    client = TestClient(create_app())
    files = {"file": ("a.md", b"enough text here to pass parsing", "text/markdown")}
    unauthenticated = client.post(_import_url(), headers=_origin(), files=files)
    assert unauthenticated.status_code == 401

    setup = client.post(
        "/api/auth/setup",
        headers=_origin(),
        json={"username": "admin", "display_name": "Admin", "password": "password1234"},
    )
    assert setup.status_code == 200
    missing_origin = client.post(_import_url(), files=files)
    assert missing_origin.status_code == 403


def test_create_task_persists_requirement_source_filename(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    with Session(get_engine()) as session:
        space = _default_space(session)
        project_id = int(space.project_id)
        space_id = int(space.id)

    response = client.post(
        f"/api/tasks?project_id={project_id}",
        headers=_origin(),
        json={
            "title": "登录需求",
            "description": "用户使用正确的账号密码登录系统并查看首页数据",
            "wiki_space_id": space_id,
            "source_filename": "login.docx",
        },
    )
    assert response.status_code == 200, response.text
    requirement_id = response.json()["requirement_id"]

    detail = client.get(
        f"/api/requirements/{requirement_id}?project_id={project_id}",
        headers=_origin(),
    )
    assert detail.status_code == 200
    assert detail.json()["source_filename"] == "login.docx"
    with Session(get_engine()) as session:
        requirement = session.get(Requirement, requirement_id)
        assert requirement is not None
        assert requirement.source_filename == "login.docx"


def test_manual_requirement_persists_source_filename(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    with Session(get_engine()) as session:
        project_id = int(_default_space(session).project_id)

    created = client.post(
        f"/api/requirements?project_id={project_id}",
        headers=_origin(),
        json={
            "title": "手工需求",
            "description": "纯手工录入的需求描述内容也支持来源文件名",
            "source_filename": "manual.md",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["source_filename"] == "manual.md"

    omitted = client.post(
        f"/api/requirements?project_id={project_id}",
        headers=_origin(),
        json={"title": "无来源需求", "description": "没有导入文件时的来源文件名保持为空"},
    )
    assert omitted.status_code == 200, omitted.text
    assert omitted.json()["source_filename"] is None
