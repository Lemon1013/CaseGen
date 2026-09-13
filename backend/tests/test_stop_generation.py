"""Manual stop of a running generation: stop endpoint + pipeline cooperative exit."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.db import get_engine
from app.main import create_app
from app.models.entities import (
    CaseDraft,
    GenerationTask,
    Project,
    Requirement,
    TaskCitation,
    TaskEvent,
    TaskRetrievalCheckpoint,
    TaskTestPointCheckpoint,
    TestPoint,
    TestPointCitation,
)
from app.services.task_pipeline import run_generate


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


def _admin_client(tmp_app_data, monkeypatch) -> TestClient:
    """Authenticated client pattern from tests/test_admin_wiki_purge.py."""
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    client = TestClient(create_app())
    response = client.post(
        "/api/auth/setup",
        headers=_origin(),
        json={"username": "admin", "display_name": "Admin", "password": "password1234"},
    )
    assert response.status_code == 200
    with Session(get_engine()) as session:
        project = session.exec(select(Project)).first()
        assert project is not None
        client.params = {"project_id": int(project.id)}
    return client


def _create_model(client: TestClient) -> int:
    response = client.post(
        "/api/models",
        json={
            "name": "stop-test-model",
            "base_url": "https://example.test/v1",
            "api_key": "sk-test-stop",
            "model_name": "fake-chat",
            "is_default": True,
        },
    )
    assert response.status_code == 200
    return response.json()["id"]


def _seed_generating_task(
    *,
    model_id: int | None = None,
    with_claim: bool = False,
    point_count: int = 2,
) -> tuple[int, int | None]:
    """Seed a task in ``generating`` with confirmed checkpoints and test points.

    Mirrors the resume path of run_generate: a confirmed retrieval checkpoint
    plus a confirmed test-point checkpoint holding the selected points.
    """

    with Session(get_engine()) as session:
        project = session.exec(select(Project)).first()
        requirement = Requirement(
            title="转账校验需求",
            description="支持普通转账与大额转账的风控校验",
            project_id=int(project.id) if project is not None else None,
        )
        session.add(requirement)
        session.commit()
        session.refresh(requirement)

        task = GenerationTask(
            requirement_id=requirement.id,
            status="generating",
            model_id=model_id,
            project_id=requirement.project_id,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        task_id = int(task.id)

        citation = TaskCitation(
            task_id=task_id,
            citation_type="wiki",
            title="转账风控规则",
            path="pages/transfer_risk.md",
            snippet="单笔超过 50,000 元需要风控审批。",
            score=0.95,
        )
        session.add(citation)
        session.commit()
        session.refresh(citation)

        retrieval = TaskRetrievalCheckpoint(
            task_id=task_id,
            attempt=1,
            status="confirmed",
            query="转账风控",
            retrieval_json=json.dumps(
                {
                    "context": {
                        "citations": [
                            {
                                "task_citation_id": citation.id,
                                "title": "转账风控规则",
                                "path": "pages/transfer_risk.md",
                            }
                        ],
                        "wiki_hits": [],
                        "source_hits": [],
                    }
                }
            ),
            selected_citation_ids_json=json.dumps([citation.id]),
        )
        session.add(retrieval)
        session.commit()
        session.refresh(retrieval)

        checkpoint = TaskTestPointCheckpoint(
            task_id=task_id,
            retrieval_checkpoint_id=retrieval.id,
            attempt=1,
            status="confirmed",
            version=1,
        )
        if with_claim:
            # An active durable lease as job_generate holds it while running.
            checkpoint.resume_claim_token = "stop-test-token"
            checkpoint.resume_claimed_at = _utcnow()
            checkpoint.resume_status = "running"
        session.add(checkpoint)
        session.commit()
        session.refresh(checkpoint)

        for index in range(point_count):
            point = TestPoint(
                task_id=task_id,
                checkpoint_id=checkpoint.id,
                stable_key=f"TP-{index + 1:03d}",
                title=f"测试点 {index + 1}",
                verification_goal="验证目标",
                dimension="positive" if index == 0 else "boundary",
                priority="P0" if index == 0 else "P1",
                sort_order=index,
                is_selected=True,
                is_excluded=False,
            )
            session.add(point)
            session.commit()
            session.refresh(point)
            if index == 1:
                session.add(TestPointCitation(test_point_id=int(point.id), citation_id=int(citation.id)))
        session.commit()
        return task_id, (int(checkpoint.id) if with_claim else None)


def test_stop_endpoint_marks_failed_and_releases_lease(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    task_id, checkpoint_id = _seed_generating_task(with_claim=True)

    response = client.post(f"/api/tasks/{task_id}/stop", headers=_origin())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_message"] == "已手动停止生成"

    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        assert task.status == "failed"
        assert task.error_message == "已手动停止生成"

        checkpoint = session.get(TaskTestPointCheckpoint, checkpoint_id)
        assert checkpoint is not None
        # The lease is released; the token itself is kept for traceability.
        assert checkpoint.resume_status == "stopped"
        assert checkpoint.resume_claim_token == "stop-test-token"

        events = session.exec(
            select(TaskEvent).where(TaskEvent.task_id == task_id)
        ).all()
        assert any(event.message == "用户手动停止生成" for event in events)

    # A second stop is rejected: the task is no longer generating.
    conflict = client.post(f"/api/tasks/{task_id}/stop", headers=_origin())
    assert conflict.status_code == 409
    assert "仅生成中的任务可以停止" in conflict.json()["detail"]


def test_stop_endpoint_rejects_non_generating_task(tmp_app_data, monkeypatch):
    client = _admin_client(tmp_app_data, monkeypatch)
    task_id, _ = _seed_generating_task()
    with Session(get_engine()) as session:
        task = session.get(GenerationTask, task_id)
        assert task is not None
        task.status = "failed"
        task.error_message = "boom"
        session.add(task)
        session.commit()

    response = client.post(f"/api/tasks/{task_id}/stop", headers=_origin())
    assert response.status_code == 409


def test_point_by_point_loop_exits_after_manual_stop(tmp_app_data, monkeypatch):
    """A stop between two point calls must abort the loop and write no draft."""
    client = TestClient(create_app())
    mid = _create_model(client)
    task_id, _ = _seed_generating_task(model_id=mid, point_count=2)

    calls: list[str] = []

    def stop_during_first_call(**kwargs):
        messages = kwargs.get("messages") or []
        user_content = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        calls.append(user_content)
        # Simulate POST /stop flipping the durable state while the worker is
        # awaiting the LLM response for the first point.
        with Session(get_engine()) as session:
            task = session.get(GenerationTask, task_id)
            assert task is not None
            task.status = "failed"
            task.error_message = "已手动停止生成"
            session.add(task)
            session.commit()
        return (
            "## TC-001 普通转账成功\n"
            "- 关联测试点：TP-001\n"
            "- 优先级：P0\n\n"
            "### 测试步骤\n"
            "1. 提交转账\n"
            "   预期结果：成功\n"
        )

    with Session(get_engine()) as session:
        gen_task = run_generate(
            session, task_id, chat_fn=stop_during_first_call, point_by_point=True
        )
        assert gen_task.status == "failed"
        assert gen_task.error_message == "已手动停止生成"

    # Only the first point was sent; the loop exited before the second call.
    assert len(calls) == 1

    with Session(get_engine()) as session:
        drafts = session.exec(select(CaseDraft).where(CaseDraft.task_id == task_id)).all()
        assert drafts == []
        messages = [
            event.message
            for event in session.exec(select(TaskEvent).where(TaskEvent.task_id == task_id)).all()
        ]
        # The completed point still left its durable progress event behind...
        assert any("测试点用例生成完成 [1/2]" in message for message in messages)
        # ...and the abort was recorded without overwriting the stop reason.
        assert any("检测到手动停止，中止后续生成" in message for message in messages)
        task = session.get(GenerationTask, task_id)
        assert task is not None
        assert task.status == "failed"


def test_complete_generation_aborts_before_draft_after_stop(tmp_app_data, monkeypatch):
    """Non point-by-point branch: stop observed after the LLM reply, no draft."""
    client = TestClient(create_app())
    mid = _create_model(client)
    task_id, _ = _seed_generating_task(model_id=mid, point_count=1)

    calls: list[str] = []

    def stop_then_reply(**kwargs):
        messages = kwargs.get("messages") or []
        user_content = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        calls.append(user_content)
        with Session(get_engine()) as session:
            task = session.get(GenerationTask, task_id)
            assert task is not None
            task.status = "failed"
            task.error_message = "已手动停止生成"
            session.add(task)
            session.commit()
        return "# 用例：余额不足下单\n\n## 测试步骤\n1. 提交限价买单\n"

    # No ``point_by_point`` attribute and no explicit flag: the pipeline takes
    # the single-call complete-case branch inside the test environment.
    with Session(get_engine()) as session:
        gen_task = run_generate(session, task_id, chat_fn=stop_then_reply)
        assert gen_task.status == "failed"
        assert gen_task.error_message == "已手动停止生成"

    assert len(calls) == 1
    with Session(get_engine()) as session:
        drafts = session.exec(select(CaseDraft).where(CaseDraft.task_id == task_id)).all()
        assert drafts == []
        messages = [
            event.message
            for event in session.exec(select(TaskEvent).where(TaskEvent.task_id == task_id)).all()
        ]
        assert any("检测到手动停止，中止后续生成" in message for message in messages)
        task = session.get(GenerationTask, task_id)
        assert task is not None
        assert task.status == "failed"
