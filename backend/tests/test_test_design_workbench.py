import hashlib
import json
import time

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import get_engine
from app.main import create_app
from app.models.entities import (
    CaseDraft,
    GenerationTask,
    TaskRetrievalCheckpoint,
    TaskTestPointCheckpoint,
    TestCase as CaseRow,
    TestPoint as PointRow,
    TestPointCaseLink as PointCaseLink,
)
from app.services.task_pipeline import run_generate


def _model(client: TestClient) -> int:
    response = client.post(
        "/api/models",
        json={
            "name": "workbench-model",
            "base_url": "https://example.test/v1",
            "api_key": "sk-test-workbench",
            "model_name": "fake",
            "is_default": True,
        },
    )
    assert response.status_code == 200
    return response.json()["id"]


def _hits(*_args, **_kwargs):
    return {
        "wiki_hits": [
            {
                "id": 101,
                "title": "余额规则",
                "path": "pages/balance.md",
                "content": "余额不足时拒绝下单。",
                "score": 0.9,
            }
        ],
        "source_hits": [
            {
                "source_chunk_id": 201,
                "document_id": 99,
                "title": "余额原文",
                "path": "documents/rules.md",
                "content": "原文规定余额不足不得提交订单。",
                "score": 0.8,
            }
        ],
    }


def _markdown(*, strict: bool = False) -> str:
    if not strict:
        return "# 生成用例\n- 关联知识：[1]\n"
    return (
        "## TC-001 余额不足\n"
        "- 优先级：P0\n"
        "- 关联测试点：TP-001\n\n"
        "### 预期结果\n余额不足时订单被拒绝。\n"
    )


def _install_hooks(monkeypatch, *, point_payload: str, markdown: str) -> None:
    monkeypatch.setattr("app.api.tasks._GENERATE_CHAT_FN", lambda **kwargs: markdown)
    monkeypatch.setattr(
        "app.services.task_pipeline._GENERATE_CHAT_FN", lambda **kwargs: markdown
    )
    monkeypatch.setattr("app.api.tasks._TEST_POINTS_CHAT_FN", lambda **kwargs: point_payload)
    monkeypatch.setattr(
        "app.services.task_pipeline._TEST_POINTS_CHAT_FN", lambda **kwargs: point_payload
    )


def _point_payload(*, two_points: bool = False, citations: list[str] | None = None) -> str:
    points = [
        {
            "stable_key": "TP-001",
            "title": "验证余额不足行为",
            "verification_goal": "验证余额不足时订单被拒绝",
            "dimension": "positive",
            "priority": "P1",
            "citation_ids": citations or [],
        }
    ]
    if two_points:
        points.append(
            {
                "stable_key": "TP-002",
                "title": "验证余额边界",
                "verification_goal": "验证余额边界输入的处理",
                "dimension": "boundary",
                "priority": "P2",
                "citation_ids": [],
            }
        )
    return json.dumps({"test_points": points}, ensure_ascii=False)


def _six_point_payload() -> str:
    return json.dumps(
        {
            "test_points": [
                {
                    "stable_key": f"TP-{index:03d}",
                    "title": f"测试点 {index}",
                    "verification_goal": f"验证目标 {index}",
                    "dimension": "positive" if index % 2 else "boundary",
                    "priority": "P1",
                    "citation_ids": [],
                }
                for index in range(1, 7)
            ]
        },
        ensure_ascii=False,
    )


def _six_case_markdown(*, mapped: int = 6) -> str:
    sections = []
    for index in range(1, 7):
        point_key = f"TP-{index:03d}" if index <= mapped else "TP-999"
        sections.append(
            f"## TC-{index:03d} 用例 {index}\n"
            "- 优先级：P1\n"
            f"- 关联测试点：{point_key}\n\n"
            "### 预期结果\n行为符合需求。"
        )
    return "\n\n".join(sections)


def _wait_for_status(client: TestClient, task_id: int, expected: set[str]) -> dict:
    current = client.get(f"/api/tasks/{task_id}").json()
    for _ in range(100):
        if current["status"] in expected:
            return current
        time.sleep(0.02)
        current = client.get(f"/api/tasks/{task_id}").json()
    return current


def _start_to_points(
    client: TestClient,
    monkeypatch,
    *,
    model_id: int,
    point_payload: str,
    markdown: str | None = None,
    test_dimensions: list[str] | None = None,
) -> tuple[int, dict]:
    _install_hooks(
        monkeypatch,
        point_payload=point_payload,
        markdown=markdown or _markdown(),
    )
    monkeypatch.setattr(
        "app.services.hybrid_retrieve.hybrid_retrieve", _hits
    )
    body = {
        "title": "余额不足下单",
        "description": "验证余额不足下单行为",
        "model_id": model_id,
    }
    if test_dimensions is not None:
        body["test_dimensions"] = test_dimensions
    created = client.post("/api/tasks", json=body)
    assert created.status_code == 200
    task_id = created.json()["id"]

    started = client.post(f"/api/tasks/{task_id}/generate")
    assert started.status_code == 200
    assert started.json()["status"] == "awaiting_confirmation"
    retrieval = client.get(f"/api/tasks/{task_id}/retrieval-checkpoint").json()
    confirmed = client.post(
        f"/api/tasks/{task_id}/retrieval-checkpoint/confirm",
        json={
            "selected_citation_ids": [item["id"] for item in retrieval["candidate_citations"]],
            "supplemental_text": "",
            "expected_version": retrieval["version"],
            "idempotency_key": f"workbench-retrieval-{task_id}-{retrieval['version']}",
        },
    )
    assert confirmed.status_code == 200
    status = _wait_for_status(
        client,
        task_id,
        {"awaiting_test_point_confirmation", "failed"},
    )
    assert status["status"] == "awaiting_test_point_confirmation", status
    checkpoint = client.get(f"/api/tasks/{task_id}/test-points")
    assert checkpoint.status_code == 200
    return task_id, checkpoint.json()


def _confirm_points(client: TestClient, task_id: int, checkpoint: dict, key: str) -> dict:
    response = client.post(
        f"/api/tasks/{task_id}/test-points/confirm",
        json={
            "points": checkpoint["points"],
            "expected_version": checkpoint["version"],
            "idempotency_key": key,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_task_create_defaults_and_reference_snapshots(tmp_app_data):
    client = TestClient(create_app())
    requirement = client.post(
        "/api/requirements",
        json={"title": "原需求", "description": "原描述", "focus_tags": ["旧标签"]},
    ).json()
    case = client.post(
        "/api/cases",
        json={
            "requirement_id": requirement["id"],
            "case_key": "TC-REF",
            "title": "参考用例",
            "content_md": "## TC-REF\n参考表达",
            "priority": "P2",
        },
    ).json()
    default_task = client.post(
        "/api/tasks",
        json={"title": "默认任务", "description": "验证默认配置"},
    )
    assert default_task.status_code == 200
    assert default_task.json()["generation_granularity"] == "standard"
    assert default_task.json()["test_dimensions"] == ["positive", "negative", "boundary"]

    manual = "## 手动参考\n仅作为表达风格参考"
    created = client.post(
        "/api/tasks",
        json={
            "requirement_id": requirement["id"],
            "focus_tags": [],
            "reference_case_ids": [case["id"]],
            "reference_text": manual,
        },
    )
    assert created.status_code == 200
    task_id = created.json()["id"]
    references = client.get(f"/api/tasks/{task_id}/references").json()
    assert len(references) == 2
    library = next(item for item in references if item["source"] == "case_library")
    manual_row = next(item for item in references if item["source"] == "manual")
    assert library["content_md_snapshot"] == "## TC-REF\n参考表达"
    assert library["content_hash"] == hashlib.sha256(library["content_md_snapshot"].encode()).hexdigest()
    assert manual_row["content_md_snapshot"] == manual

    updated = client.patch(
        f"/api/cases/{case['id']}",
        json={"content_md": "## TC-REF\n已修改", "expected_revision": case["revision"]},
    )
    assert updated.status_code == 200
    unchanged = client.get(f"/api/tasks/{task_id}/references").json()[0]
    assert unchanged["content_md_snapshot"] == "## TC-REF\n参考表达"
    assert client.get(f"/api/requirements/{requirement['id']}").json()["focus_tags"] == []

    archived = client.post(f"/api/cases/{case['id']}/archive")
    assert archived.status_code == 200
    assert client.post(
        "/api/tasks",
        json={"reference_case_ids": [case["id"]]},
    ).status_code == 422
    assert client.post(
        "/api/tasks",
        json={"reference_case_ids": [999999]},
    ).status_code == 422
    assert client.post(
        "/api/tasks",
        json={"reference_case_ids": list(range(1, 12))},
    ).status_code == 422
    assert client.post(
        "/api/tasks",
        json={"reference_text": "x" * 16001},
    ).status_code == 422


def test_requirement_optimize_returns_structured_editable_result(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    model_id = _model(client)
    monkeypatch.setattr(
        "app.api.tasks._REQUIREMENT_OPTIMIZE_CHAT_FN",
        lambda **kwargs: json.dumps(
            {"title": "优化标题", "description": "优化描述", "questions": ["确认边界"]},
            ensure_ascii=False,
        ),
    )
    response = client.post(
        "/api/tasks/requirement-optimize",
        json={"title": "原标题", "description": "原描述", "model_id": model_id},
    )
    assert response.status_code == 200
    assert response.json() == {
        "title": "优化标题",
        "description": "优化描述",
        "questions": ["确认边界"],
        "prompt_type": "requirement_optimize",
    }


def test_test_point_fallback_is_editable_checkpoint(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    model_id = _model(client)
    task_id, checkpoint = _start_to_points(
        client,
        monkeypatch,
        model_id=model_id,
        point_payload="not valid json",
    )
    assert checkpoint["status"] == "pending"
    assert checkpoint["points"][0]["stable_key"] == "TP-001"
    messages = " ".join(
        item["message"] for item in client.get(f"/api/tasks/{task_id}/events").json()
    )
    assert "fallback" in messages


def test_test_point_edit_confirm_version_and_idempotency(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    model_id = _model(client)
    task_id, checkpoint = _start_to_points(
        client,
        monkeypatch,
        model_id=model_id,
        point_payload=_point_payload(two_points=False),
        test_dimensions=["positive", "negative", "boundary", "security"],
    )
    invalid = dict(checkpoint["points"][0])
    invalid["citation_ids"] = [999999]
    response = client.put(
        f"/api/tasks/{task_id}/test-points",
        json={"expected_version": checkpoint["version"], "points": [invalid]},
    )
    assert response.status_code == 422
    stale = client.put(
        f"/api/tasks/{task_id}/test-points",
        json={"expected_version": 999, "points": checkpoint["points"]},
    )
    assert stale.status_code == 409

    first = dict(checkpoint["points"][0])
    first.update({"priority": "P0", "dimension": "security"})
    second = {
        "stable_key": "TP-002",
        "title": "排除的边界点",
        "verification_goal": "保留但排除该边界点",
        "dimension": "boundary",
        "priority": "P2",
        "sort_order": 1,
        "is_selected": True,
        "is_excluded": True,
        "citation_ids": [],
    }
    edited = client.put(
        f"/api/tasks/{task_id}/test-points",
        json={
            "expected_version": checkpoint["version"],
            "points": [first, second],
        },
    )
    assert edited.status_code == 200
    edited_checkpoint = edited.json()
    assert edited_checkpoint["version"] == checkpoint["version"] + 1
    assert {item["stable_key"] for item in edited_checkpoint["points"]} == {"TP-001", "TP-002"}
    assert edited_checkpoint["points"][0]["priority"] == "P0"
    assert edited_checkpoint["points"][0]["dimension"] == "security"
    assert edited_checkpoint["points"][1]["is_excluded"] is True

    confirmed = _confirm_points(
        client,
        task_id,
        edited_checkpoint,
        f"point-confirm-{task_id}",
    )
    assert confirmed["status"] in {"generating", "generated"}
    assert _wait_for_status(client, task_id, {"generated"})["status"] == "generated"
    repeated = client.post(
        f"/api/tasks/{task_id}/test-points/confirm",
        json={
            "points": edited_checkpoint["points"],
            "expected_version": edited_checkpoint["version"],
            "idempotency_key": f"point-confirm-{task_id}",
        },
    )
    assert repeated.status_code == 200
    different_key = client.post(
        f"/api/tasks/{task_id}/test-points/confirm",
        json={
            "points": edited_checkpoint["points"],
            "expected_version": edited_checkpoint["version"],
            "idempotency_key": "different-confirmation",
        },
    )
    assert different_key.status_code == 409


def test_new_task_strict_finalize_and_deterministic_coverage(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    model_id = _model(client)
    task_id, checkpoint = _start_to_points(
        client,
        monkeypatch,
        model_id=model_id,
        point_payload=_point_payload(two_points=True, citations=["1", "S1"]),
        markdown=_markdown(strict=True),
    )
    _confirm_points(client, task_id, checkpoint, f"coverage-confirm-{task_id}")
    generated = _wait_for_status(client, task_id, {"generated", "failed"})
    assert generated["status"] == "generated"
    finalized = client.post(f"/api/tasks/{task_id}/finalize")
    assert finalized.status_code == 200
    assert finalized.json()["status"] == "finalized"

    coverage = client.get(f"/api/tasks/{task_id}/coverage")
    assert coverage.status_code == 200
    body = coverage.json()
    assert body["selected_test_points"] == 2
    assert body["covered_test_points"] == 1
    assert body["uncovered_test_points"] == 1
    assert body["coverage_percent"] == 50.0
    assert any(item["stable_key"] == "TP-002" and not item["covered"] for item in body["points"])
    cases = client.get("/api/cases", params={"priority": "P0"}).json()
    assert len(cases) == 1
    assert cases[0]["priority"] == "P0"
    with Session(get_engine()) as session:
        links = session.exec(select(PointCaseLink)).all()
        assert len(links) == 1


def test_generated_draft_coverage_is_available_before_and_after_finalize(
    tmp_app_data, monkeypatch
):
    client = TestClient(create_app())
    model_id = _model(client)
    task_id, checkpoint = _start_to_points(
        client,
        monkeypatch,
        model_id=model_id,
        point_payload=_six_point_payload(),
        markdown=_six_case_markdown(),
    )
    _confirm_points(client, task_id, checkpoint, f"six-point-coverage-{task_id}")
    assert _wait_for_status(client, task_id, {"generated"})["status"] == "generated"

    draft_coverage = client.get(f"/api/tasks/{task_id}/coverage")
    assert draft_coverage.status_code == 200
    assert draft_coverage.json()["covered_test_points"] == 6
    assert draft_coverage.json()["uncovered_test_points"] == 0
    assert draft_coverage.json()["coverage_percent"] == 100.0
    assert all(point["covered"] for point in draft_coverage.json()["points"])
    assert all(point["case_ids"] == [] for point in draft_coverage.json()["points"])
    with Session(get_engine()) as session:
        assert session.exec(select(CaseRow)).all() == []
        assert session.exec(select(PointCaseLink)).all() == []

    finalized = client.post(f"/api/tasks/{task_id}/finalize")
    assert finalized.status_code == 200
    final_coverage = client.get(f"/api/tasks/{task_id}/coverage").json()
    assert final_coverage["covered_test_points"] == 6
    assert final_coverage["uncovered_test_points"] == 0
    assert final_coverage["coverage_percent"] == 100.0
    assert all(point["covered"] and point["case_ids"] for point in final_coverage["points"])


def test_generated_draft_leaves_unmapped_current_point_uncovered(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    model_id = _model(client)
    task_id, checkpoint = _start_to_points(
        client,
        monkeypatch,
        model_id=model_id,
        point_payload=_six_point_payload(),
        markdown=_six_case_markdown(mapped=5),
    )
    _confirm_points(client, task_id, checkpoint, f"partial-draft-coverage-{task_id}")
    assert _wait_for_status(client, task_id, {"generated"})["status"] == "generated"

    coverage = client.get(f"/api/tasks/{task_id}/coverage").json()
    assert coverage["selected_test_points"] == 6
    assert coverage["covered_test_points"] == 5
    assert coverage["uncovered_test_points"] == 1
    uncovered = [point["stable_key"] for point in coverage["points"] if not point["covered"]]
    assert uncovered == ["TP-006"]


def test_regenerated_draft_and_finalize_only_bind_current_checkpoint(tmp_app_data):
    client = TestClient(create_app())
    task_id = client.post(
        "/api/tasks",
        json={"title": "重生成覆盖", "description": "验证同名测试点版本隔离"},
    ).json()["id"]
    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        old_checkpoint = TaskTestPointCheckpoint(
            task_id=task_id,
            attempt=1,
            status="confirmed",
        )
        session.add(old_checkpoint)
        session.flush()
        old_point = PointRow(
            task_id=task_id,
            checkpoint_id=int(old_checkpoint.id),
            stable_key="TP-001",
            title="旧测试点",
            verification_goal="旧目标",
            dimension="positive",
            priority="P1",
        )
        session.add(old_point)
        session.add(CaseDraft(task_id=task_id, version=1, content_md=_markdown(strict=True)))
        task.status = "generated"
        session.add(task)
        session.commit()
        old_point_id = int(old_point.id)

    assert client.get(f"/api/tasks/{task_id}/coverage").json()["covered_test_points"] == 1

    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        current_checkpoint = TaskTestPointCheckpoint(
            task_id=task_id,
            attempt=2,
            status="confirmed",
        )
        session.add(current_checkpoint)
        session.flush()
        current_point = PointRow(
            task_id=task_id,
            checkpoint_id=int(current_checkpoint.id),
            stable_key="TP-001",
            title="当前测试点",
            verification_goal="当前目标",
            dimension="positive",
            priority="P1",
        )
        session.add(current_point)
        task.status = "generating"
        session.add(task)
        session.commit()
        current_point_id = int(current_point.id)

    generating_coverage = client.get(f"/api/tasks/{task_id}/coverage").json()
    assert generating_coverage["covered_test_points"] == 0
    assert generating_coverage["points"][0]["title"] == "当前测试点"

    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        session.add(CaseDraft(task_id=task_id, version=2, content_md=_markdown(strict=True)))
        task.status = "generated"
        session.add(task)
        session.commit()

    assert client.get(f"/api/tasks/{task_id}/coverage").json()["covered_test_points"] == 1
    finalized = client.post(f"/api/tasks/{task_id}/finalize")
    assert finalized.status_code == 200
    with Session(get_engine()) as session:
        links = session.exec(select(PointCaseLink)).all()
        assert [int(link.test_point_id) for link in links] == [current_point_id]
        assert all(int(link.test_point_id) != old_point_id for link in links)


def test_new_task_strict_finalize_rejects_missing_key_or_priority(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    model_id = _model(client)
    task_id, checkpoint = _start_to_points(
        client,
        monkeypatch,
        model_id=model_id,
        point_payload=_point_payload(),
        markdown="## TC-001 缺少元数据\n正文\n",
    )
    _confirm_points(client, task_id, checkpoint, f"strict-reject-{task_id}")
    assert _wait_for_status(client, task_id, {"generated"})["status"] == "generated"
    response = client.post(f"/api/tasks/{task_id}/finalize")
    assert response.status_code == 400
    assert "priority" in response.json()["detail"] or "test point" in response.json()["detail"]


def test_legacy_finalize_and_old_generating_recovery(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    model_id = _model(client)
    legacy = client.post(
        "/api/tasks",
        json={"title": "旧任务", "description": "旧描述", "model_id": model_id},
    ).json()
    with Session(get_engine()) as session:
        task = session.get(GenerationTask, legacy["id"])
        assert task is not None
        task.status = "generated"
        session.add(CaseDraft(task_id=task.id, version=1, content_md="# 旧 Markdown"))
        session.add(task)
        session.commit()
    finalized = client.post(f"/api/tasks/{legacy['id']}/finalize")
    assert finalized.status_code == 200
    assert finalized.json()["status"] == "finalized"

    task = client.post(
        "/api/tasks",
        json={"title": "恢复任务", "description": "恢复旧生成状态", "model_id": model_id},
    ).json()
    point = _point_payload()
    _install_hooks(monkeypatch, point_payload=point, markdown=_markdown())
    monkeypatch.setattr("app.services.hybrid_retrieve.hybrid_retrieve", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not retrieve")))
    with Session(get_engine()) as session:
        row = session.get(GenerationTask, task["id"])
        assert row is not None
        row.status = "generating"
        session.add(
            TaskRetrievalCheckpoint(
                task_id=row.id,
                attempt=1,
                status="confirmed",
                query="恢复",
                retrieval_json=json.dumps({"context": {"citations": [], "wiki_hits": [], "source_hits": []}}),
                selected_citation_ids_json="[]",
            )
        )
        session.add(row)
        session.commit()
        run_generate(session, int(row.id), chat_fn=lambda **kwargs: point)
        session.refresh(row)
        assert row.status == "awaiting_test_point_confirmation"
