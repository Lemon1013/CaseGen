import json

from fastapi.testclient import TestClient
from sqlmodel import Session


def _app(tmp_app_data):
    from app.main import create_app

    return create_app()


def _space(client: TestClient, name: str) -> dict:
    response = client.post("/api/wiki-spaces", json={"name": name})
    assert response.status_code == 201
    return response.json()


def test_csv_and_json_import_are_generic_and_space_isolated(tmp_app_data):
    client = TestClient(_app(tmp_app_data))
    first, second = _space(client, "First"), _space(client, "Second")
    csv_response = client.post(
        "/api/data-pools",
        json={
            "wiki_space_id": first["id"], "name": "orders", "source_kind": "csv",
            "content": "scenario,account,price\nORDER_VALID_001,A001,上下五档内价格",
        },
    )
    assert csv_response.status_code == 200
    assert csv_response.json()["latest_revision"]["records"][0]["price"] == "上下五档内价格"
    assert csv_response.json()["latest_revision"]["record_count"] == 1
    assert "raw_text" not in csv_response.json()["latest_revision"]
    json_response = client.post(
        "/api/data-pools",
        json={"wiki_space_id": second["id"], "name": "objects", "source_kind": "json", "content": '[{"arbitrary":{"x":1}}]'},
    )
    assert json_response.status_code == 200
    assert json_response.json()["latest_revision"]["records"][0]["arbitrary"] == {"x": 1}
    assert len(client.get(f'/api/data-pools?wiki_space_id={first["id"]}').json()) == 1
    assert len(client.get(f'/api/data-pools?wiki_space_id={second["id"]}').json()) == 1
    assert client.get(f'/api/data-pools/{csv_response.json()["id"]}?wiki_space_id={second["id"]}').status_code == 404


def test_delete_data_pool_requires_archive_and_removes_revisions(tmp_app_data):
    from app.db import get_engine
    from app.models.entities import DataPool, DataPoolRevision
    from sqlmodel import select

    client = TestClient(_app(tmp_app_data))
    first, second = _space(client, "Delete pool"), _space(client, "Other space")
    pool = client.post("/api/data-pools", json={
        "wiki_space_id": first["id"], "name": "temporary", "source_kind": "json", "content": '[{"x":1}]',
    }).json()
    pool_id = pool["id"]
    revision_id = pool["latest_revision"]["id"]

    active = client.delete(f'/api/data-pools/{pool_id}?wiki_space_id={first["id"]}')
    assert active.status_code == 409
    assert "先归档" in active.json()["detail"]
    assert client.delete(f'/api/data-pools/{pool_id}?wiki_space_id={second["id"]}').status_code == 404
    with Session(get_engine()) as session:
        assert session.get(DataPool, pool_id) is not None
        assert session.get(DataPoolRevision, revision_id) is not None

    assert client.post(f'/api/data-pools/{pool_id}/archive?wiki_space_id={first["id"]}').status_code == 200
    deleted = client.delete(f'/api/data-pools/{pool_id}?wiki_space_id={first["id"]}')
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}
    assert client.delete(f'/api/data-pools/{pool_id}?wiki_space_id={first["id"]}').status_code == 404
    with Session(get_engine()) as session:
        assert session.get(DataPool, pool_id) is None
        assert session.exec(select(DataPoolRevision).where(DataPoolRevision.data_pool_id == pool_id)).all() == []


def test_delete_data_pool_rejects_render_history_references(tmp_app_data):
    from app.db import get_engine
    from app.models.entities import DataPool, PlatformRenderRun

    client = TestClient(_app(tmp_app_data))
    space = _space(client, "Referenced pools")
    platform = client.post("/api/platforms", json={
        "wiki_space_id": space["id"], "name": "A", "artifact_topology": "combined",
    }).json()
    variant = client.post(
        f'/api/platforms/{platform["id"]}/variants?wiki_space_id={space["id"]}', json={"name": "default"},
    ).json()

    for reference_key in ("pool_id", "revision_id"):
        pool = client.post("/api/data-pools", json={
            "wiki_space_id": space["id"], "name": reference_key, "source_kind": "json", "content": '[{"x":1}]',
        }).json()
        reference_id = pool["id"] if reference_key == "pool_id" else pool["latest_revision"]["id"]
        with Session(get_engine()) as session:
            session.add(PlatformRenderRun(
                wiki_space_id=space["id"], platform_id=platform["id"], variant_id=variant["id"],
                input_snapshot_json=json.dumps({"data_refs": [{reference_key: reference_id}]}),
            ))
            session.commit()
        assert client.post(f'/api/data-pools/{pool["id"]}/archive?wiki_space_id={space["id"]}').status_code == 200
        response = client.delete(f'/api/data-pools/{pool["id"]}?wiki_space_id={space["id"]}')
        assert response.status_code == 409
        assert "生成历史引用" in response.json()["detail"]
        with Session(get_engine()) as session:
            assert session.get(DataPool, pool["id"]) is not None


def _semantic_case(space_id: int) -> int:
    from app.db import get_engine
    from app.models.entities import GenerationTask, Requirement, TestCase

    with Session(get_engine()) as session:
        requirement = Requirement(title="fund", description="fund subscription")
        session.add(requirement); session.flush()
        task = GenerationTask(requirement_id=requirement.id, wiki_space_id=space_id, status="completed")
        session.add(task); session.flush()
        case = TestCase(requirement_id=requirement.id, case_key="TC-001", title="基金申购", content_md="## TC-001\n申购成功", source_task_id=task.id)
        session.add(case); session.commit(); session.refresh(case)
        return int(case.id)


def test_example_direct_only_sends_selected_variant_and_saves_separated_artifacts(tmp_app_data, monkeypatch):
    from app.api import platform_data
    from app.db import get_engine
    from app.models.entities import ModelConfig, PlatformArtifact, PlatformRenderRun
    from sqlmodel import select

    client = TestClient(_app(tmp_app_data))
    space = _space(client, "Trading")
    with Session(get_engine()) as session:
        session.add(ModelConfig(name="fake", base_url="http://invalid", api_key="x", model_name="fake", is_default=True)); session.commit()
    platform = client.post("/api/platforms", json={"wiki_space_id": space["id"], "name": "A", "artifact_topology": "separated"}).json()
    fund = client.post(f'/api/platforms/{platform["id"]}/variants?wiki_space_id={space["id"]}', json={"name": "基金", "applicability": "fund"}).json()
    stock = client.post(f'/api/platforms/{platform["id"]}/variants?wiki_space_id={space["id"]}', json={"name": "股票", "applicability": "stock"}).json()
    client.post(f'/api/platforms/{platform["id"]}/variants/{fund["id"]}/examples?wiki_space_id={space["id"]}', json={"kind": "case", "content": "FUND_ONLY_EXAMPLE"})
    client.post(f'/api/platforms/{platform["id"]}/variants/{stock["id"]}/examples?wiki_space_id={space["id"]}', json={"kind": "case", "content": "STOCK_MUST_NOT_LEAK"})
    pool = client.post("/api/data-pools", json={
        "wiki_space_id": space["id"], "name": "sensitive", "source_kind": "json",
        "content": '[{"account":"SENSITIVE_NEVER_SENT"}]',
    }).json()
    revision_id = pool["latest_revision"]["id"]
    captured = {}

    def fake_chat(messages, **_kwargs):
        captured["messages"] = messages
        return json.dumps({"artifacts": [{"kind": "case", "filename": "cases.txt", "media_type": "text/plain", "content": "CASE PLAIN\nKEEP"}, {"kind": "data", "filename": "data.csv", "media_type": "text/csv", "content": "dataset_id,value\nD-001,抽象数据"}], "warnings": []})

    monkeypatch.setattr(platform_data, "_PLATFORM_CHAT_FN", fake_chat)
    case_id = _semantic_case(space["id"])
    response = client.post("/api/platform-renders/example-direct", json={"wiki_space_id": space["id"], "platform_id": platform["id"], "variant_id": fund["id"], "test_case_ids": [case_id], "data_pool_revision_ids": [revision_id]})
    assert response.status_code == 200, response.text
    context = json.dumps(captured["messages"], ensure_ascii=False)
    assert "FUND_ONLY_EXAMPLE" in context
    assert "STOCK_MUST_NOT_LEAK" not in context
    assert "SENSITIVE_NEVER_SENT" not in context
    assert {item["kind"] for item in response.json()["artifacts"]} == {"case", "data"}
    artifacts = {item["kind"]: item for item in response.json()["artifacts"]}
    assert (artifacts["case"]["media_type"], artifacts["case"]["content"]) == (
        "text/plain", "CASE PLAIN\nKEEP",
    )
    assert (artifacts["data"]["media_type"], artifacts["data"]["content"]) == (
        "text/csv", "dataset_id,value\nD-001,抽象数据",
    )
    assert "外层 JSON 只是传输封套" in captured["messages"][0]["content"]
    assert response.json()["status"] == "completed"
    opt_in = client.post("/api/platform-renders/example-direct", json={
        "wiki_space_id": space["id"], "platform_id": platform["id"], "variant_id": fund["id"],
        "test_case_ids": [case_id], "data_pool_revision_ids": [revision_id],
        "include_data_values": True, "data_sample_limit": 1,
    })
    assert opt_in.status_code == 200
    assert "SENSITIVE_NEVER_SENT" in json.dumps(captured["messages"], ensure_ascii=False)
    with Session(get_engine()) as session:
        runs = session.exec(select(PlatformRenderRun).order_by(PlatformRenderRun.id)).all()
        persisted = session.exec(
            select(PlatformArtifact).where(PlatformArtifact.run_id == runs[0].id)
        ).all()
        assert {(item.media_type, item.content) for item in persisted} == {
            ("text/plain", "CASE PLAIN\nKEEP"),
            ("text/csv", "dataset_id,value\nD-001,抽象数据"),
        }
        assert all("SENSITIVE_NEVER_SENT" not in run.input_snapshot_json for run in runs)
        assert json.loads(runs[0].input_snapshot_json)["include_data_values"] is False
        assert json.loads(runs[1].input_snapshot_json)["data_refs"][0]["sample_count"] == 1
    def failing_chat(*_args, **_kwargs):
        raise RuntimeError("injected call failure")
    monkeypatch.setattr(platform_data, "_PLATFORM_CHAT_FN", failing_chat)
    failed = client.post("/api/platform-renders/example-direct", json={
        "wiki_space_id": space["id"], "platform_id": platform["id"], "variant_id": fund["id"],
        "test_case_ids": [case_id],
    })
    assert failed.status_code == 502
    with Session(get_engine()) as session:
        latest = session.exec(select(PlatformRenderRun).order_by(PlatformRenderRun.id.desc())).first()
        assert latest.status == "failed"
        assert latest.error_message == "injected call failure"


def test_example_direct_rejects_empty_selected_variant_without_calling_llm_or_creating_run(tmp_app_data, monkeypatch):
    from app.api import platform_data
    from app.db import get_engine
    from app.models.entities import PlatformRenderRun
    from sqlmodel import select

    client = TestClient(_app(tmp_app_data))
    space = _space(client, "Empty selected variant")
    platform = client.post(
        "/api/platforms",
        json={"wiki_space_id": space["id"], "name": "A", "artifact_topology": "combined"},
    ).json()
    empty_variant = client.post(
        f'/api/platforms/{platform["id"]}/variants?wiki_space_id={space["id"]}',
        json={"name": "empty"},
    ).json()
    other_variant = client.post(
        f'/api/platforms/{platform["id"]}/variants?wiki_space_id={space["id"]}',
        json={"name": "with-example"},
    ).json()
    example_response = client.post(
        f'/api/platforms/{platform["id"]}/variants/{other_variant["id"]}/examples?wiki_space_id={space["id"]}',
        json={"kind": "combined", "content": "{}"},
    )
    assert example_response.status_code == 200

    llm_called = False

    def fake_chat(*_args, **_kwargs):
        nonlocal llm_called
        llm_called = True
        return '{}'

    monkeypatch.setattr(platform_data, "_PLATFORM_CHAT_FN", fake_chat)
    response = client.post(
        "/api/platform-renders/example-direct",
        json={
            "wiki_space_id": space["id"],
            "platform_id": platform["id"],
            "variant_id": empty_variant["id"],
            "test_case_ids": [_semantic_case(space["id"])],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "当前示例类型没有平台示例，请先保存至少一个平台示例"
    assert llm_called is False
    with Session(get_engine()) as session:
        assert session.exec(select(PlatformRenderRun)).all() == []


def test_combined_topology_rejects_invalid_json_and_persists_failed_run(tmp_app_data, monkeypatch):
    from app.api import platform_data
    from app.db import get_engine
    from app.models.entities import ModelConfig, PlatformRenderRun
    from sqlmodel import select

    client = TestClient(_app(tmp_app_data))
    space = _space(client, "Combined")
    with Session(get_engine()) as session:
        session.add(ModelConfig(name="fake", base_url="http://invalid", api_key="x", model_name="fake", is_default=True)); session.commit()
    platform = client.post("/api/platforms", json={"wiki_space_id": space["id"], "name": "B", "artifact_topology": "combined"}).json()
    variant = client.post(f'/api/platforms/{platform["id"]}/variants?wiki_space_id={space["id"]}', json={"name": "fund"}).json()
    client.post(f'/api/platforms/{platform["id"]}/variants/{variant["id"]}/examples?wiki_space_id={space["id"]}', json={"kind": "combined", "content": "{}"})
    monkeypatch.setattr(platform_data, "_PLATFORM_CHAT_FN", lambda *_args, **_kwargs: '{"artifacts":[{"kind":"combined","filename":"cases.json","content":"not json"}]}' )
    response = client.post("/api/platform-renders/example-direct", json={"wiki_space_id": space["id"], "platform_id": platform["id"], "variant_id": variant["id"], "test_case_ids": [_semantic_case(space["id"])]})
    assert response.status_code == 502
    with Session(get_engine()) as session:
        run = session.exec(select(PlatformRenderRun)).one()
        assert run.status == "failed"
        assert "invalid json" in (run.error_message or "").lower()
    oversized = {"artifacts": [
        {"kind": "combined", "filename": f"case-{index}.json", "content": "{}"}
        for index in range(21)
    ]}
    monkeypatch.setattr(platform_data, "_PLATFORM_CHAT_FN", lambda *_args, **_kwargs: json.dumps(oversized))
    response = client.post("/api/platform-renders/example-direct", json={"wiki_space_id": space["id"], "platform_id": platform["id"], "variant_id": variant["id"], "test_case_ids": [_semantic_case(space["id"])]})
    assert response.status_code == 502
    with Session(get_engine()) as session:
        runs = session.exec(select(PlatformRenderRun).order_by(PlatformRenderRun.id)).all()
        assert runs[-1].status == "failed"
        assert "at most 20" in (runs[-1].error_message or "")


def test_missing_model_marks_render_failed(tmp_app_data):
    from app.db import get_engine
    from app.models.entities import PlatformRenderRun
    from sqlmodel import select

    client = TestClient(_app(tmp_app_data))
    space = _space(client, "No model")
    platform = client.post("/api/platforms", json={"wiki_space_id": space["id"], "name": "B", "artifact_topology": "combined"}).json()
    variant = client.post(f'/api/platforms/{platform["id"]}/variants?wiki_space_id={space["id"]}', json={"name": "fund"}).json()
    client.post(f'/api/platforms/{platform["id"]}/variants/{variant["id"]}/examples?wiki_space_id={space["id"]}', json={"kind": "combined", "content": "{}"})
    response = client.post("/api/platform-renders/example-direct", json={"wiki_space_id": space["id"], "platform_id": platform["id"], "variant_id": variant["id"], "test_case_ids": [_semantic_case(space["id"])]})
    assert response.status_code == 503
    with Session(get_engine()) as session:
        run = session.exec(select(PlatformRenderRun)).one()
        assert run.status == "failed"
        assert "ModelConfig" in (run.error_message or "")


def test_semantic_case_listing_is_space_isolated(tmp_app_data):
    client = TestClient(_app(tmp_app_data))
    first, second = _space(client, "Cases A"), _space(client, "Cases B")
    first_id = _semantic_case(first["id"])
    second_id = _semantic_case(second["id"])
    first_rows = client.get(f'/api/platform-semantic-cases?wiki_space_id={first["id"]}').json()
    second_rows = client.get(f'/api/platform-semantic-cases?wiki_space_id={second["id"]}').json()
    assert [row["id"] for row in first_rows] == [first_id]
    assert [row["id"] for row in second_rows] == [second_id]


def test_csv_validation_rejects_inconsistent_rows():
    from app.api.platform_data import _validate_artifacts
    import pytest

    with pytest.raises(ValueError, match="inconsistent CSV"):
        _validate_artifacts("separated", [
            {"kind": "case", "filename": "cases.csv", "content": "id,title\n1"},
            {"kind": "data", "filename": "data.csv", "content": "id,value\n1,x"},
        ])


def test_artifact_filename_validation_rejects_unsafe_names():
    from app.api.platform_data import _validate_artifacts
    import pytest

    for filename in ("../cases.json", "dir/cases.json", "dir\\cases.json", ".", "..", "bad\nname.json"):
        with pytest.raises(ValueError, match="filename is unsafe"):
            _validate_artifacts("combined", [{
                "kind": "combined", "filename": filename,
                "media_type": "application/json", "content": "{}",
            }])


def test_archived_space_rejects_platform_configuration_writes(tmp_app_data):
    from app.db import get_engine
    from app.models.entities import Project, WikiSpace

    client = TestClient(_app(tmp_app_data))
    space = _space(client, "Archive writes")
    platform = client.post("/api/platforms", json={
        "wiki_space_id": space["id"], "name": "Platform", "artifact_topology": "combined",
    }).json()
    variant = client.post(
        f'/api/platforms/{platform["id"]}/variants?wiki_space_id={space["id"]}',
        json={"name": "Type"},
    ).json()
    example = client.post(
        f'/api/platforms/{platform["id"]}/variants/{variant["id"]}/examples?wiki_space_id={space["id"]}',
        json={"kind": "combined", "content": "{}"},
    ).json()
    with Session(get_engine()) as session:
        owner = session.get(Project, space["project_id"])
        replacement = WikiSpace(name="Archive replacement", slug="archive-replacement", scope="project", project_id=owner.id)
        session.add(replacement); session.flush()
        owner.default_wiki_space_id = replacement.id
        session.add(owner); session.commit()
    assert client.post(f'/api/wiki-spaces/{space["id"]}/archive').status_code == 200

    responses = [
        client.patch(f'/api/platforms/{platform["id"]}?wiki_space_id={space["id"]}', json={"name": "No"}),
        client.delete(f'/api/platforms/{platform["id"]}?wiki_space_id={space["id"]}'),
        client.post(f'/api/platforms/{platform["id"]}/variants?wiki_space_id={space["id"]}', json={"name": "No"}),
        client.patch(f'/api/platforms/{platform["id"]}/variants/{variant["id"]}?wiki_space_id={space["id"]}', json={"name": "No"}),
        client.delete(f'/api/platforms/{platform["id"]}/variants/{variant["id"]}?wiki_space_id={space["id"]}'),
        client.post(f'/api/platforms/{platform["id"]}/variants/{variant["id"]}/examples?wiki_space_id={space["id"]}', json={"kind": "combined", "content": "{}"}),
        client.patch(f'/api/platforms/{platform["id"]}/variants/{variant["id"]}/examples/{example["id"]}?wiki_space_id={space["id"]}', json={"content": "new"}),
        client.delete(f'/api/platforms/{platform["id"]}/variants/{variant["id"]}/examples/{example["id"]}?wiki_space_id={space["id"]}'),
    ]
    assert {response.status_code for response in responses} == {409}


def test_platform_example_type_and_example_crud_with_safe_delete(tmp_app_data):
    client = TestClient(_app(tmp_app_data))
    first, second = _space(client, "CRUD A"), _space(client, "CRUD B")
    platform = client.post("/api/platforms", json={
        "wiki_space_id": first["id"], "name": "Old", "description": "before",
        "artifact_topology": "combined",
    }).json()
    updated = client.patch(
        f'/api/platforms/{platform["id"]}?wiki_space_id={first["id"]}',
        json={"name": "New", "artifact_topology": "hybrid"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "New"
    assert client.patch(
        f'/api/platforms/{platform["id"]}?wiki_space_id={second["id"]}', json={"name": "leak"},
    ).status_code == 404
    variant = client.post(
        f'/api/platforms/{platform["id"]}/variants?wiki_space_id={first["id"]}',
        json={"name": "基金", "applicability": "old"},
    ).json()
    assert client.patch(
        f'/api/platforms/{platform["id"]}/variants/{variant["id"]}?wiki_space_id={first["id"]}',
        json={"name": "基金申购", "applicability": "new"},
    ).json()["name"] == "基金申购"
    example = client.post(
        f'/api/platforms/{platform["id"]}/variants/{variant["id"]}/examples?wiki_space_id={first["id"]}',
        json={"kind": "combined", "name": "sample", "media_type": "application/json", "content": "{}"},
    ).json()
    changed = client.patch(
        f'/api/platforms/{platform["id"]}/variants/{variant["id"]}/examples/{example["id"]}?wiki_space_id={first["id"]}',
        json={"media_type": " text/plain ", "content": "  changed\n"},
    )
    assert changed.status_code == 200
    assert changed.json()["content"] == "  changed\n"
    assert changed.json()["media_type"] == "text/plain"
    assert client.delete(
        f'/api/platforms/{platform["id"]}/variants/{variant["id"]}/examples/{example["id"]}?wiki_space_id={second["id"]}'
    ).status_code == 404
    assert client.delete(
        f'/api/platforms/{platform["id"]}/variants/{variant["id"]}?wiki_space_id={first["id"]}'
    ).status_code == 200
    assert client.delete(f'/api/platforms/{platform["id"]}?wiki_space_id={first["id"]}').status_code == 200


def test_external_markdown_keywords_history_and_delete_conflict(tmp_app_data, monkeypatch):
    from app.api import platform_data
    from app.db import get_engine
    from app.models.entities import ModelConfig, PlatformRenderRun
    from sqlmodel import select

    client = TestClient(_app(tmp_app_data))
    first, second = _space(client, "Render A"), _space(client, "Render B")
    with Session(get_engine()) as session:
        session.add(ModelConfig(name="fake", base_url="http://invalid", api_key="x", model_name="fake", is_default=True))
        session.commit()
    platform = client.post("/api/platforms", json={
        "wiki_space_id": first["id"], "name": "B", "artifact_topology": "combined",
    }).json()
    variant = client.post(
        f'/api/platforms/{platform["id"]}/variants?wiki_space_id={first["id"]}', json={"name": "基金"},
    ).json()
    example = client.post(
        f'/api/platforms/{platform["id"]}/variants/{variant["id"]}/examples?wiki_space_id={first["id"]}',
        json={"kind": "combined", "content": "{}"},
    ).json()
    captured = {"messages": []}
    def fake_chat(messages, **_kwargs):
        captured["messages"].append(messages)
        return '{"artifacts":[{"kind":"combined","filename":"cases.json","media_type":"application/json","content":"{}"}]}'
    monkeypatch.setattr(platform_data, "_PLATFORM_CHAT_FN", fake_chat)
    markdown = "# EXTERNAL-ONLY\n基金外部用例"
    response = client.post("/api/platform-renders/example-direct", json={
        "wiki_space_id": first["id"], "platform_id": platform["id"], "variant_id": variant["id"],
        "external_case_markdown": markdown, "keywords": [" KW-SECRET ", "回归", "KW-SECRET"],
    })
    assert response.status_code == 200, response.text
    sent = json.dumps(captured["messages"][0], ensure_ascii=False)
    assert "EXTERNAL-ONLY" in sent
    user_payload = json.loads(captured["messages"][0][-1]["content"])
    assert user_payload["keywords"] == ["KW-SECRET", "回归"]
    with Session(get_engine()) as session:
        run = session.exec(select(PlatformRenderRun)).one()
        manifest = json.loads(run.input_snapshot_json)
        assert markdown not in run.input_snapshot_json
        assert manifest["external_case_markdown"]["length"] == len(markdown)
        assert "KW-SECRET" not in run.input_snapshot_json
        assert len(manifest["keyword_refs"]) == 2
        assert manifest["platform_snapshot"]["name"] == "B"
        assert manifest["variant_snapshot"]["name"] == "基金"
        old_example_hash = manifest["example_refs"][0]["content_hash"]
        assert old_example_hash == example["content_hash"]
    assert client.patch(f'/api/platforms/{platform["id"]}?wiki_space_id={first["id"]}', json={"name": "B renamed"}).status_code == 200
    assert client.patch(f'/api/platforms/{platform["id"]}/variants/{variant["id"]}?wiki_space_id={first["id"]}', json={"name": "类型 renamed"}).status_code == 200
    history = client.get(f'/api/platform-renders?wiki_space_id={first["id"]}').json()
    assert len(history) == 1
    assert history[0]["platform_name"] == "B"
    assert history[0]["variant_name"] == "基金"
    assert any("人工确认平台字段" in item for item in history[0]["warnings"])
    assert client.get(f'/api/platform-renders?wiki_space_id={second["id"]}').json() == []
    changed = client.patch(
        f'/api/platforms/{platform["id"]}/variants/{variant["id"]}/examples/{example["id"]}?wiki_space_id={first["id"]}',
        json={"content": "NEW-EXAMPLE-CONTENT"},
    )
    assert changed.status_code == 200
    assert changed.json()["content_hash"] != old_example_hash
    with Session(get_engine()) as session:
        old_run = session.exec(select(PlatformRenderRun).order_by(PlatformRenderRun.id)).first()
        assert json.loads(old_run.input_snapshot_json)["example_refs"][0]["content_hash"] == old_example_hash
    delete_response = client.delete(
        f'/api/platforms/{platform["id"]}/variants/{variant["id"]}/examples/{example["id"]}?wiki_space_id={first["id"]}'
    )
    assert delete_response.status_code == 409
    assert "cannot be deleted" in delete_response.json()["detail"]
    second_render = client.post("/api/platform-renders/example-direct", json={
        "wiki_space_id": first["id"], "platform_id": platform["id"], "variant_id": variant["id"],
        "external_case_markdown": markdown,
    })
    assert second_render.status_code == 200, second_render.text
    second_payload = json.loads(captured["messages"][1][-1]["content"])
    assert second_payload["selected_variant_examples"][0]["content"] == "NEW-EXAMPLE-CONTENT"
    assert client.delete(
        f'/api/platforms/{platform["id"]}/variants/{variant["id"]}?wiki_space_id={first["id"]}'
    ).status_code == 409
    assert client.delete(f'/api/platforms/{platform["id"]}?wiki_space_id={first["id"]}').status_code == 409


def test_platform_case_management_copy_sync_preview_and_isolation(tmp_app_data, monkeypatch):
    from app.api import platform_data
    from app.db import get_engine
    from app.models.entities import ManagedPlatformCase, ModelConfig, PlatformArtifact, PlatformRenderRun
    from sqlmodel import select

    client = TestClient(_app(tmp_app_data))
    first, second = _space(client, "Managed A"), _space(client, "Managed B")
    with Session(get_engine()) as session:
        session.add(ModelConfig(name="fake", base_url="http://invalid", api_key="x", model_name="fake", is_default=True))
        session.commit()
    platform = client.post("/api/platforms", json={
        "wiki_space_id": first["id"], "name": "Target", "artifact_topology": "combined",
    }).json()
    variant = client.post(
        f'/api/platforms/{platform["id"]}/variants?wiki_space_id={first["id"]}', json={"name": "基金"},
    ).json()
    client.post(
        f'/api/platforms/{platform["id"]}/variants/{variant["id"]}/examples?wiki_space_id={first["id"]}',
        json={"kind": "combined", "content": "{}"},
    )
    monkeypatch.setattr(platform_data, "_PLATFORM_CHAT_FN", lambda *_args, **_kwargs: json.dumps({
        "artifacts": [{"kind": "combined", "filename": "generated.json", "media_type": "application/json", "content": '{"old":true}'}]
    }))
    rendered = client.post("/api/platform-renders/example-direct", json={
        "wiki_space_id": first["id"], "platform_id": platform["id"], "variant_id": variant["id"],
        "external_case_markdown": "# case",
    })
    assert rendered.status_code == 200, rendered.text
    generated_artifact = rendered.json()["artifacts"][0]
    page = client.get(f'/api/platform-cases?wiki_space_id={first["id"]}&limit=1&offset=0').json()
    assert page["total"] == 1 and page["limit"] == 1 and page["offset"] == 0
    listed = page["items"]
    assert len(listed) == 1
    assert listed[0]["source_artifact_id"] == generated_artifact["id"]
    assert "content" not in listed[0]

    managed_id = listed[0]["id"]
    changed = client.patch(
        f'/api/platform-cases/{managed_id}?wiki_space_id={first["id"]}',
        json={"content": '{"copy":true}', "name": "Editable copy"},
    )
    assert changed.status_code == 200
    assert changed.json()["json_value"] == {"copy": True}
    with Session(get_engine()) as session:
        assert session.get(PlatformArtifact, generated_artifact["id"]).content == '{"old":true}'
        run = session.exec(select(PlatformRenderRun)).one()
        late_artifact = PlatformArtifact(
            run_id=run.id, kind="attachment", filename="late.txt", media_type="text/plain",
            content="late", content_hash=platform_data._hash("late"),
        )
        session.add(late_artifact); session.commit()
    assert client.get(f'/api/platform-cases?wiki_space_id={first["id"]}').json()["total"] == 2
    assert client.get(f'/api/platform-cases?wiki_space_id={first["id"]}').json()["total"] == 2
    with Session(get_engine()) as session:
        assert platform_data._sync_managed_platform_cases(session, first["id"]) == 0
    second_page = client.get(f'/api/platform-cases?wiki_space_id={first["id"]}&limit=1&offset=1').json()
    assert second_page["total"] == 2 and len(second_page["items"]) == 1
    searched = client.get(f'/api/platform-cases?wiki_space_id={first["id"]}&search=late').json()
    assert searched["total"] == 1 and searched["items"][0]["filename"] == "late.txt"

    csv_case = client.post("/api/platform-cases", json={
        "wiki_space_id": first["id"], "platform_id": platform["id"], "variant_id": variant["id"],
        "filename": "manual.csv", "name": "CSV", "kind": "data", "media_type": "text/csv",
        "content": 'id,note\n1,"a,b"',
    })
    assert csv_case.status_code == 200, csv_case.text
    assert csv_case.json()["render_mode"] == "csv"
    assert csv_case.json()["csv_headers"] == ["id", "note"]
    assert csv_case.json()["csv_rows"] == [["1", "a,b"]]
    scalar = client.post("/api/platform-cases", json={
        "wiki_space_id": first["id"], "platform_id": platform["id"], "filename": "scalar.json",
        "kind": "attachment", "media_type": "application/json", "content": "42",
    })
    assert scalar.status_code == 200
    assert scalar.json()["json_value"] == 42
    assert client.get(f'/api/platform-cases/{scalar.json()["id"]}?wiki_space_id={second["id"]}').status_code == 404
    archived = client.delete(f'/api/platform-cases/{scalar.json()["id"]}?wiki_space_id={first["id"]}')
    assert archived.json()["status"] == "archived"
    assert client.patch(
        f'/api/platform-cases/{scalar.json()["id"]}?wiki_space_id={first["id"]}', json={"name": "blocked"},
    ).status_code == 409
    assert client.post(f'/api/platform-cases/{scalar.json()["id"]}/restore?wiki_space_id={first["id"]}').json()["status"] == "active"

    deep_json = "[" * 25 + "0" + "]" * 25
    deep = client.post("/api/platform-cases", json={
        "wiki_space_id": first["id"], "platform_id": platform["id"], "filename": "deep.json",
        "kind": "attachment", "media_type": "application/json", "content": deep_json,
    })
    assert deep.status_code == 200
    assert deep.json()["json_truncated"] is True
    assert deep.json()["content"] == deep_json

    for body in (
        {"filename": "../bad.json", "kind": "combined", "media_type": "application/json", "content": "{}"},
        {"filename": "bad.json", "kind": "combined", "media_type": "application/json", "content": "not-json"},
        {"filename": "bad.csv", "kind": "data", "media_type": "text/csv", "content": "a,b\n1"},
    ):
        response = client.post("/api/platform-cases", json={
            "wiki_space_id": first["id"], "platform_id": platform["id"], **body,
        })
        assert response.status_code == 422, response.text


def test_managed_case_references_block_platform_and_example_type_delete(tmp_app_data):
    client = TestClient(_app(tmp_app_data))
    space = _space(client, "Managed refs")
    platform = client.post("/api/platforms", json={
        "wiki_space_id": space["id"], "name": "Target", "artifact_topology": "combined",
    }).json()
    variant = client.post(
        f'/api/platforms/{platform["id"]}/variants?wiki_space_id={space["id"]}', json={"name": "Type"},
    ).json()
    created = client.post("/api/platform-cases", json={
        "wiki_space_id": space["id"], "platform_id": platform["id"], "variant_id": variant["id"],
        "filename": "manual.txt", "kind": "case", "media_type": "text/plain", "content": "manual",
    })
    assert created.status_code == 200
    assert client.delete(
        f'/api/platforms/{platform["id"]}/variants/{variant["id"]}?wiki_space_id={space["id"]}'
    ).status_code == 409
    assert client.delete(f'/api/platforms/{platform["id"]}?wiki_space_id={space["id"]}').status_code == 409


def test_artifact_limits_and_deep_json_are_rejected(monkeypatch):
    from app.api import platform_data
    import pytest

    base = {"kind": "combined", "filename": "case.json", "media_type": "application/json", "content": "{}"}
    with pytest.raises(ValueError, match="at most 20"):
        platform_data._validate_artifacts("combined", [base] * 21)
    with pytest.raises(ValueError, match="1 MB"):
        platform_data._validate_artifacts("combined", [{**base, "content": "x" * 1_000_001, "media_type": "text/plain", "filename": "large.txt"}])
    with pytest.raises(ValueError, match="5 MB"):
        platform_data._validate_artifacts("combined", [
            {**base, "filename": f"large-{index}.txt", "media_type": "text/plain", "content": "x" * 900_000}
            for index in range(6)
        ])
    monkeypatch.setattr(platform_data.json, "loads", lambda _value: (_ for _ in ()).throw(RecursionError()))
    with pytest.raises(ValueError, match="nesting is too deep"):
        platform_data._validate_artifacts("combined", [base])
