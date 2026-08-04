from fastapi.testclient import TestClient

from app.main import create_app


def test_upload_markdown(tmp_app_data):
    client = TestClient(create_app())
    content = "# 余额规则\n余额不足应拒绝下单".encode("utf-8")
    files = {"file": ("rules.md", content, "text/markdown")}
    r = client.post("/api/documents", files=files)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("parsed", "uploaded", "ready")
    assert body["filename"] == "rules.md"
    assert body["char_count"] > 0
    assert body["sha256"]
    assert "raw/sources/" in body["stored_path"]

    listed = client.get("/api/documents").json()
    assert len(listed) >= 1
    assert any(item["id"] == body["id"] for item in listed)

    detail = client.get(f"/api/documents/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["filename"] == "rules.md"


def test_reject_bad_extension(tmp_app_data):
    client = TestClient(create_app())
    files = {"file": ("malware.exe", b"not-a-doc", "application/octet-stream")}
    r = client.post("/api/documents", files=files)
    assert r.status_code == 400
    assert "extension" in r.json()["detail"].lower()


def test_upload_docx(tmp_app_data, tmp_path):
    from docx import Document

    docx_path = tmp_path / "rules.docx"
    doc = Document()
    doc.add_heading("余额规则", level=1)
    doc.add_paragraph("余额不足应拒绝下单")
    doc.save(docx_path)

    client = TestClient(create_app())
    files = {
        "file": (
            "rules.docx",
            docx_path.read_bytes(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    }
    r = client.post("/api/documents", files=files)
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "rules.docx"
    assert body["status"] == "parsed"
    assert body["char_count"] > 0
