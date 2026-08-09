"""Ensure every public API route is registered and basic 404 paths respond."""

from fastapi.testclient import TestClient

from app.main import create_app


EXPECTED_PATHS = {
    ("GET", "/api/health"),
    ("POST", "/api/documents"),
    ("GET", "/api/documents"),
    ("GET", "/api/documents/{document_id}"),
    ("POST", "/api/documents/{document_id}/ingest"),
    ("POST", "/api/models"),
    ("GET", "/api/models"),
    ("GET", "/api/models/{model_id}"),
    ("PUT", "/api/models/{model_id}"),
    ("DELETE", "/api/models/{model_id}"),
    ("POST", "/api/models/{model_id}/ping"),
    ("GET", "/api/prompts"),
    ("POST", "/api/prompts"),
    ("GET", "/api/prompts/{prompt_id}"),
    ("PUT", "/api/prompts/{prompt_id}"),
    ("POST", "/api/requirements"),
    ("GET", "/api/requirements"),
    ("GET", "/api/requirements/{requirement_id}"),
    ("POST", "/api/tasks"),
    ("GET", "/api/tasks"),
    ("GET", "/api/tasks/{task_id}"),
    ("POST", "/api/tasks/{task_id}/generate"),
    ("POST", "/api/tasks/{task_id}/review"),
    ("POST", "/api/tasks/{task_id}/optimize-prompt"),
    ("POST", "/api/tasks/{task_id}/apply-prompt"),
    ("POST", "/api/tasks/{task_id}/regenerate"),
    ("POST", "/api/tasks/{task_id}/finalize"),
    ("GET", "/api/tasks/{task_id}/drafts"),
    ("GET", "/api/tasks/{task_id}/citations"),
    ("GET", "/api/tasks/{task_id}/events"),
    ("GET", "/api/tasks/{task_id}/reviews"),
    ("GET", "/api/tasks/{task_id}/revisions"),
    ("GET", "/api/ingest-jobs/{job_id}"),
    ("POST", "/api/ingest-jobs/{job_id}/retry-failed-windows"),
    ("GET", "/api/wiki/pages"),
    ("GET", "/api/wiki/pages/{page_id}"),
    ("GET", "/api/wiki/reviews"),
    ("GET", "/api/wiki/reviews/{review_id}"),
    ("POST", "/api/wiki/reviews/{review_id}/approve"),
    ("POST", "/api/wiki/reviews/{review_id}/reject"),
    ("POST", "/api/wiki/reviews/{review_id}/acknowledge"),
    ("GET", "/api/wiki/index"),
    ("POST", "/api/wiki/retrieve"),
}


def test_openapi_registers_all_expected_routes(tmp_app_data):
    client = TestClient(create_app())
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    registered = set()
    for path, methods in paths.items():
        for method in methods:
            if method in {"get", "post", "put", "delete", "patch"}:
                registered.add((method.upper(), path))

    missing = EXPECTED_PATHS - registered
    assert not missing, f"Missing routes: {sorted(missing)}"


def test_not_found_resources(tmp_app_data):
    client = TestClient(create_app())
    assert client.get("/api/documents/99999").status_code == 404
    assert client.get("/api/models/99999").status_code == 404
    assert client.get("/api/prompts/99999").status_code == 404
    assert client.get("/api/requirements/99999").status_code == 404
    assert client.get("/api/tasks/99999").status_code == 404
    assert client.get("/api/wiki/pages/99999").status_code == 404
    assert client.get("/api/ingest-jobs/99999").status_code == 404
    assert client.post("/api/documents/99999/ingest").status_code == 404
