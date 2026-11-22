"""Phase 3 backend refactoring tests.

Covers:
1. Pluggable RetrievalStrategy injection (per-call and process-wide)
2. Per-point LLMError lean-context fallback retry without affecting other points
3. parse_case_detail handling of ``### 待确认项`` without polluting step expected
4. recover_generation_jobs across retrieving/regenerating/reviewing/optimizing
5. Global background-job concurrency semaphore queueing, no deadlock, and error release
6. list_tasks O(1) SQL query count (<= 12 for 15 tasks) and private prompt sanitization parity
7. Shared stream_broker / build_sse_streaming_response across task & agent SSE endpoints
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, select

from app import config
import app.api.agent_chat as agent_chat_api
import app.api.tasks as tasks_api
import app.services.task_jobs as task_jobs
import app.services.task_pipeline as task_pipeline
from app.db import get_engine
from app.main import create_app
from app.models.entities import (
    AgentConversation,
    CaseDraft,
    GenerationTask,
    Project,
    PromptTemplate,
    Requirement,
    ReviewResult,
    TaskCitation,
    TaskEvent,
    TaskReferenceCase,
    TaskRetrievalCheckpoint,
    TaskTestPointCheckpoint,
    TestCase as _TestCaseRow,
    TestPoint as _TestPointRow,
    TestPointCitation as _TestPointCitationRow,
    User,
)
from app.services.agent_stream import AgentStreamBroker
from app.services.auth import hash_password
from app.services.case_management import parse_case_detail, split_case_draft
from app.services.knowledge_providers import KnowledgeHit, ProjectKnowledgeSearchResult
from app.services.pipeline import (
    LLMError,
    RetrievalStrategy,
    get_retrieval_strategy,
    run_generate,
    set_retrieval_strategy,
)
from app.services.stream_broker import (
    BaseStreamBroker,
    DEFAULT_SSE_HEADERS,
    build_sse_streaming_response,
)
from app.services.task_stream import TaskStreamBroker


def _origin() -> dict[str, str]:
    return {"Origin": "http://testserver"}


def _create_model(client: TestClient, name: str = "phase3-model") -> int:
    resp = client.post(
        "/api/models",
        json={
            "name": name,
            "base_url": "https://example.test/v1",
            "api_key": "sk-phase3-test",
            "model_name": "fake-chat",
            "is_default": True,
        },
    )
    assert resp.status_code == 200, resp.text
    return int(resp.json()["id"])


def _parse_sse_frames(raw_body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in raw_body.strip().split("\n\n"):
        if not block.strip() or block.strip().startswith(":"):
            continue
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                fields[key] = value
        if "event" in fields:
            frames.append(
                {
                    "id": int(fields["id"]) if "id" in fields else None,
                    "event": fields["event"],
                    "data": json.loads(fields["data"]),
                }
            )
    return frames


# ---------------------------------------------------------------------------
# 1. RetrievalStrategy custom injection
# ---------------------------------------------------------------------------


class _CustomMultiRoundStrategy:
    """Custom RetrievalStrategy simulating future Agent multi-round retrieval."""

    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.calls: list[dict[str, Any]] = []

    def retrieve(
        self,
        session: Session,
        *,
        project_id: int | None,
        query: str,
        top_k: int | None = None,
    ) -> ProjectKnowledgeSearchResult:
        self.calls.append({"project_id": project_id, "query": query, "top_k": top_k})
        return ProjectKnowledgeSearchResult(
            configured=True,
            empty_reason="none",
            message="",
            hits=[
                KnowledgeHit(
                    source_id=77,
                    source_name=f"agent-multi-round-{self.marker}",
                    provider_type="agent_retrieval",
                    chunk_id=f"chunk-{self.marker}-1",
                    title=f"自定义检索命中-{self.marker}",
                    file_path=f"rules/{self.marker}.md",
                    document_path=f"rules/{self.marker}.md",
                    heading_path="风控规则 > 限额校验",
                    score=0.98,
                    weighted_score=0.98,
                    snippet=f"条款 4.2.1：单笔限额校验规则（{self.marker}）。",
                    evidence_snippet=f"条款 4.2.1：单笔限额校验规则（{self.marker}）。",
                )
            ],
            warnings=[],
        )


def test_retrieval_strategy_injection_per_call_and_global(tmp_app_data, monkeypatch):
    client = TestClient(create_app())
    model_id = _create_model(client)

    # Ensure default external provider search is never called when custom strategy is active.
    def _forbid_default_search(*args, **kwargs):
        raise AssertionError("Default search_project_knowledge must not be called")

    monkeypatch.setattr(task_pipeline, "search_project_knowledge", _forbid_default_search)

    per_call_strategy = _CustomMultiRoundStrategy("per-call")
    global_strategy = _CustomMultiRoundStrategy("global")
    assert isinstance(per_call_strategy, RetrievalStrategy)
    assert isinstance(global_strategy, RetrievalStrategy)

    with Session(get_engine()) as session:
        project = session.exec(select(Project)).first()
        assert project is not None and project.id is not None

        req1 = Requirement(title="显式策略注入需求", description="验证自定义单次注入检索策略")
        session.add(req1)
        session.flush()
        task1 = GenerationTask(
            requirement_id=int(req1.id),
            project_id=int(project.id),
            model_id=model_id,
            status="draft",
        )
        session.add(task1)
        session.commit()
        session.refresh(task1)

        out1 = run_generate(session, int(task1.id), retrieval_strategy=per_call_strategy)
        assert out1.status == "awaiting_confirmation"
        assert len(per_call_strategy.calls) == 1
        assert per_call_strategy.calls[0]["project_id"] == int(project.id)

        citations1 = session.exec(
            select(TaskCitation).where(TaskCitation.task_id == task1.id)
        ).all()
        assert len(citations1) == 1
        assert citations1[0].title == "自定义检索命中-per-call"

        cp1 = session.exec(
            select(TaskRetrievalCheckpoint).where(TaskRetrievalCheckpoint.task_id == task1.id)
        ).one()
        assert cp1.status == "pending"
        assert json.loads(cp1.candidate_citation_ids_json) == [citations1[0].id]

        # Global strategy registration via set_retrieval_strategy
        original = get_retrieval_strategy()
        try:
            set_retrieval_strategy(global_strategy)
            assert get_retrieval_strategy() is global_strategy

            req2 = Requirement(title="全局策略注入需求", description="验证全局注册检索策略")
            session.add(req2)
            session.flush()
            task2 = GenerationTask(
                requirement_id=int(req2.id),
                project_id=int(project.id),
                model_id=model_id,
                status="draft",
            )
            session.add(task2)
            session.commit()
            session.refresh(task2)

            out2 = run_generate(session, int(task2.id))
            assert out2.status == "awaiting_confirmation"
            assert len(global_strategy.calls) == 1
            citations2 = session.exec(
                select(TaskCitation).where(TaskCitation.task_id == task2.id)
            ).all()
            assert len(citations2) == 1
            assert citations2[0].title == "自定义检索命中-global"
        finally:
            set_retrieval_strategy(None)
            assert type(get_retrieval_strategy()) is type(original)


# ---------------------------------------------------------------------------
# 2. Per-point LLMError lean-context fallback retry
# ---------------------------------------------------------------------------


def test_single_point_llm_error_triggers_lean_fallback_and_keeps_other_points_intact(
    tmp_app_data,
):
    client = TestClient(create_app())
    model_id = _create_model(client)

    long_excerpt = "规则条款3.5.2详细说明：" + ("超长证据上下文" * 600)
    long_supplemental = "人工补充背景：" + ("补充说明内容" * 300)
    ref_marker = "REF_CASE_TEMPLATE_MARKER_XYZ"

    with Session(get_engine()) as session:
        req = Requirement(title="分点降级重试需求", description="验证单测试点失败精简重试")
        session.add(req)
        session.flush()

        task = GenerationTask(
            requirement_id=int(req.id),
            status="generating",
            model_id=model_id,
        )
        session.add(task)
        session.flush()
        task_id = int(task.id)

        citation = TaskCitation(
            task_id=task_id,
            citation_type="external",
            title="超长风控规范",
            path="rules/risk_long.md",
            snippet=long_excerpt[:300],
            content_excerpt=long_excerpt,
            score=0.96,
        )
        session.add(citation)
        session.flush()

        session.add(
            TaskReferenceCase(
                task_id=task_id,
                title_snapshot="参考用例样例",
                content_md_snapshot=f"## TC-REF - 参考用例\n{ref_marker}",
                content_hash="hash-ref-1",
                source="manual",
            )
        )

        rc = TaskRetrievalCheckpoint(
            task_id=task_id,
            attempt=1,
            status="confirmed",
            query="风控规范",
            supplemental_text=long_supplemental,
            retrieval_json=json.dumps(
                {
                    "context": {
                        "citations": [
                            {
                                "task_citation_id": citation.id,
                                "title": citation.title,
                                "path": citation.path,
                            }
                        ],
                        "wiki_hits": [],
                        "source_hits": [],
                        "external_hits": [],
                    }
                },
                ensure_ascii=False,
            ),
            selected_citation_ids_json=json.dumps([citation.id]),
        )
        session.add(rc)
        session.flush()

        tpc = TaskTestPointCheckpoint(
            task_id=task_id,
            retrieval_checkpoint_id=int(rc.id),
            attempt=1,
            status="confirmed",
            version=1,
        )
        session.add(tpc)
        session.flush()

        tp1 = _TestPointRow(
            task_id=task_id,
            checkpoint_id=int(tpc.id),
            stable_key="TP-001",
            title="超长上下文触发降级的测试点",
            verification_goal="验证主生成报错后精简重试成功",
            dimension="positive",
            priority="P0",
            sort_order=0,
            is_selected=True,
            is_excluded=False,
        )
        tp2 = _TestPointRow(
            task_id=task_id,
            checkpoint_id=int(tpc.id),
            stable_key="TP-002",
            title="正常完成的第二个测试点",
            verification_goal="验证其他测试点不受前一个测试点降级影响",
            dimension="boundary",
            priority="P1",
            sort_order=1,
            is_selected=True,
            is_excluded=False,
        )
        session.add(tp1)
        session.add(tp2)
        session.flush()
        session.add(_TestPointCitationRow(test_point_id=int(tp1.id), citation_id=int(citation.id)))
        session.add(_TestPointCitationRow(test_point_id=int(tp2.id), citation_id=int(citation.id)))
        session.commit()

        call_records: list[dict[str, Any]] = []

        def flaky_point_chat(messages, model=None):
            joined = "\n".join(m.get("content", "") for m in messages)
            system_text = messages[0].get("content", "") if messages else ""
            call_records.append(
                {
                    "system": system_text,
                    "joined": joined,
                    "has_ref": ref_marker in joined,
                    "is_lean": "# 精简重试补充" in system_text,
                }
            )
            # First call for TP-001 fails with LLMError -> triggers lean retry
            if len(call_records) == 1:
                assert "TP-001" in joined
                assert ref_marker in joined
                raise LLMError("upstream 502 context too long")
            # Second call is lean retry for TP-001
            if len(call_records) == 2:
                assert "TP-001" in joined
                assert "# 精简重试补充" in system_text
                assert ref_marker not in joined
                assert len(joined) < len(call_records[0]["joined"])
                return (
                    "## TC-001 - 精简重试后生成用例\n"
                    "- 优先级：P0\n"
                    "- 关联测试点：TP-001\n"
                    "### 测试步骤\n1. 提交校验请求。\n   - 预期：返回成功\n"
                )
            # Third call is normal primary generation for TP-002
            assert "TP-002" in joined
            assert "# 精简重试补充" not in system_text
            assert ref_marker in joined
            return (
                "## TC-001 - 第二个测试点用例\n"
                "- 优先级：P1\n"
                "- 关联测试点：TP-002\n"
                "### 测试步骤\n1. 输入边界值。\n   - 预期：边界校验通过\n"
            )

        result = run_generate(session, task_id, chat_fn=flaky_point_chat)
        assert result.status == "generated"
        assert len(call_records) == 3
        assert call_records[0]["has_ref"] is True and call_records[0]["is_lean"] is False
        assert call_records[1]["has_ref"] is False and call_records[1]["is_lean"] is True
        assert call_records[2]["has_ref"] is True and call_records[2]["is_lean"] is False

        draft = session.exec(select(CaseDraft).where(CaseDraft.task_id == task_id)).one()
        assert draft.prompt_version_ref is not None
        assert draft.prompt_version_ref.endswith("|lean_fallback")
        assert "## TC-001" in draft.content_md
        assert "## TC-002" in draft.content_md

        events = session.exec(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id.asc())
        ).all()
        fallback_events = [
            ev for ev in events if "主生成失败，精简上下文重试" in ev.message
        ]
        assert len(fallback_events) == 1
        fb_detail = json.loads(fallback_events[0].detail_json or "{}")
        assert fb_detail["point_key"] == "TP-001"
        assert fb_detail["lean_fallback"] is True
        assert fb_detail["dropped_reference_cases"] is True
        assert fb_detail["lean_citation_chars"] <= 2400

        done_events = [ev for ev in events if "生成完成，draft v1" in ev.message]
        assert len(done_events) == 1
        done_detail = json.loads(done_events[0].detail_json or "{}")
        assert done_detail["lean_fallback"] is True
        assert done_detail["lean_fallback_points"] == ["TP-001"]


# ---------------------------------------------------------------------------
# 3. parse_case_detail with ### 待确认项
# ---------------------------------------------------------------------------


def test_parse_case_detail_pending_items_not_mixed_into_last_step_expected():
    raw_md = """## TC-101 - 交易时段撤单校验
- 优先级：P0
- 类型：功能
- 关联测试点：TP-001
- 验证目标：验证连续竞价阶段撤单成功且解冻资金

### 前置条件
- 已报入未成交买单

### 测试步骤
1. 调用撤单接口提交撤单指令。
   - 预期：系统返回撤单已受理
2. 查询委托与资金流水。
   - 预期：委托状态变更为已撤，冻结资金实时释放

### 待确认项
- 需确认集合竞价最后 1 分钟是否允许撤单
- 需确认部分成交后的手续费退还精度规则
"""
    sections = split_case_draft(raw_md)
    assert len(sections) == 1
    detail = parse_case_detail(sections[0])

    assert len(detail["steps"]) == 2
    assert detail["steps"][-1]["expected"] == "委托状态变更为已撤，冻结资金实时释放"
    assert "待确认项" not in detail["steps"][-1]["expected"]
    assert "集合竞价" not in detail["steps"][-1]["expected"]
    assert detail["pending_items"] == [
        "需确认集合竞价最后 1 分钟是否允许撤单",
        "需确认部分成交后的手续费退还精度规则",
    ]


# ---------------------------------------------------------------------------
# 4. recover_generation_jobs across retrieving/regenerating/reviewing/optimizing
# ---------------------------------------------------------------------------


def test_recover_generation_jobs_covers_retrieving_regenerating_reviewing_and_optimizing(
    tmp_app_data, monkeypatch
):
    # Initialize schema via create_app
    create_app()

    broker = TaskStreamBroker()
    monkeypatch.setattr(task_jobs, "task_stream", broker)

    called: dict[str, list[tuple[Any, ...]]] = {
        "generate": [],
        "regenerate": [],
        "review": [],
        "optimize": [],
    }
    done_events: list[threading.Event] = []

    def _make_stub(bucket: str):
        def _stub(*args, **kwargs):
            called[bucket].append(args)
            if done_events:
                pass

        return _stub

    # Track spawned threads so we can join deterministically without leaks.
    spawned_threads: list[threading.Thread] = []
    real_thread = threading.Thread

    class _TrackingThread(real_thread):
        def start(self) -> None:
            spawned_threads.append(self)
            super().start()

    monkeypatch.setattr(task_jobs.threading, "Thread", _TrackingThread)
    monkeypatch.setattr(task_jobs, "job_generate", _make_stub("generate"))
    monkeypatch.setattr(task_jobs, "job_regenerate", _make_stub("regenerate"))
    monkeypatch.setattr(task_jobs, "job_review", _make_stub("review"))
    monkeypatch.setattr(task_jobs, "job_optimize_prompt", _make_stub("optimize"))

    with Session(get_engine()) as session:
        def _new_task(title: str, status: str) -> GenerationTask:
            req = Requirement(title=title, description=title)
            session.add(req)
            session.flush()
            row = GenerationTask(requirement_id=int(req.id), status=status)
            session.add(row)
            session.flush()
            return row

        t_retrieving = _new_task("recover-retrieving", "retrieving")
        t_regenerating = _new_task("recover-regenerating", "regenerating")
        t_reviewing_ok = _new_task("recover-reviewing-with-draft", "reviewing")
        t_reviewing_no_draft = _new_task("recover-reviewing-no-draft", "reviewing")
        t_optimizing_ok = _new_task("recover-optimizing-with-review", "optimizing")
        t_optimizing_no_review = _new_task("recover-optimizing-no-review", "optimizing")

        draft_for_review = CaseDraft(
            task_id=int(t_reviewing_ok.id),
            version=1,
            content_md="## TC-001 - 草稿\n内容",
        )
        session.add(draft_for_review)
        session.flush()

        draft_for_opt = CaseDraft(
            task_id=int(t_optimizing_ok.id),
            version=1,
            content_md="## TC-001 - 优化草稿\n内容",
        )
        session.add(draft_for_opt)
        session.flush()
        session.add(
            ReviewResult(
                task_id=int(t_optimizing_ok.id),
                draft_id=int(draft_for_opt.id),
                score=75,
                verdict="needs_revision",
                payload_json=json.dumps({"score": 75, "verdict": "needs_revision"}),
            )
        )
        session.commit()

        ids_map = {
            "retrieving": int(t_retrieving.id),
            "regenerating": int(t_regenerating.id),
            "reviewing_ok": int(t_reviewing_ok.id),
            "reviewing_no_draft": int(t_reviewing_no_draft.id),
            "optimizing_ok": int(t_optimizing_ok.id),
            "optimizing_no_review": int(t_optimizing_no_review.id),
        }

    recovered = task_jobs.recover_generation_jobs()
    for thread in spawned_threads:
        thread.join(timeout=5.0)
        assert not thread.is_alive()

    assert set(recovered) == {
        ids_map["retrieving"],
        ids_map["regenerating"],
        ids_map["reviewing_ok"],
        ids_map["optimizing_ok"],
    }
    assert called["generate"] == [(ids_map["retrieving"], False)]
    assert called["regenerate"] == [(ids_map["regenerating"],)]
    assert called["review"] == [(ids_map["reviewing_ok"],)]
    assert called["optimize"] == [(ids_map["optimizing_ok"],)]

    # Live streams were opened for retrieving and regenerating tasks.
    ret_snap = broker.snapshot(ids_map["retrieving"])
    reg_snap = broker.snapshot(ids_map["regenerating"])
    assert ret_snap is not None and ret_snap["status"] == "retrieving"
    assert reg_snap is not None and reg_snap["status"] == "regenerating"

    # Tasks missing prerequisite artifacts transitioned safely instead of hanging.
    with Session(get_engine()) as session:
        rev_failed = session.get(GenerationTask, ids_map["reviewing_no_draft"])
        assert rev_failed is not None
        assert rev_failed.status == "failed"
        assert rev_failed.error_message and "评审中断" in rev_failed.error_message

        opt_reverted = session.get(GenerationTask, ids_map["optimizing_no_review"])
        assert opt_reverted is not None
        assert opt_reverted.status == "reviewed"
        assert opt_reverted.error_message is None

        for tid in ids_map.values():
            rec_events = session.exec(
                select(TaskEvent).where(
                    TaskEvent.task_id == tid,
                    TaskEvent.step == "recovery",
                )
            ).all()
            assert len(rec_events) == 1


# ---------------------------------------------------------------------------
# 5. Global background-job concurrency semaphore
# ---------------------------------------------------------------------------


def test_task_concurrency_semaphore_queues_without_deadlock_and_releases_on_error(
    tmp_app_data, monkeypatch
):
    create_app()
    broker = TaskStreamBroker()
    monkeypatch.setattr(task_jobs, "task_stream", broker)
    monkeypatch.setattr(task_jobs.logger, "exception", lambda *args, **kwargs: None)

    with Session(get_engine()) as session:
        t_ids: list[int] = []
        for idx in range(4):
            req = Requirement(title=f"sem-req-{idx}", description=f"desc-{idx}")
            session.add(req)
            session.flush()
            task = GenerationTask(requirement_id=int(req.id), status="generating")
            session.add(task)
            session.flush()
            t_ids.append(int(task.id))
        session.commit()

    try:
        task_jobs.set_task_max_concurrent(1)

        # --- Part A: second job queues behind first job, max concurrency == 1, no deadlock ---
        t1_id, t2_id, t3_id, t4_id = t_ids
        broker.start(t1_id, status="generating")
        broker.start(t2_id, status="generating")

        first_entered = threading.Event()
        allow_first_finish = threading.Event()
        second_entered = threading.Event()
        active_lock = threading.Lock()
        active_count = 0
        max_active = 0
        order: list[int] = []

        def controlled_run_generate(session, task_id, chat_fn=None, auto_review=False):
            nonlocal active_count, max_active
            with active_lock:
                active_count += 1
                max_active = max(max_active, active_count)
                order.append(int(task_id))
            try:
                if int(task_id) == t1_id:
                    first_entered.set()
                    assert allow_first_finish.wait(timeout=5.0)
                elif int(task_id) == t2_id:
                    second_entered.set()
            finally:
                with active_lock:
                    active_count -= 1

        monkeypatch.setattr(task_jobs, "run_generate", controlled_run_generate)

        th1 = threading.Thread(target=task_jobs.job_generate, args=(t1_id,))
        th2 = threading.Thread(target=task_jobs.job_generate, args=(t2_id,))
        th1.start()
        assert first_entered.wait(timeout=5.0)

        th2.start()
        # Wait briefly and verify task 2 is queued rather than entering concurrently.
        deadline = time.monotonic() + 2.0
        saw_queue_notice = False
        while time.monotonic() < deadline:
            notices = [
                ev for ev in broker.events_after(t2_id, 0) if ev.event == "notice"
            ]
            if any("排队等待执行槽位" in str(ev.payload.get("message", "")) for ev in notices):
                saw_queue_notice = True
                break
            time.sleep(0.02)

        assert not second_entered.is_set(), "Task 2 must wait while concurrency limit is 1"
        assert saw_queue_notice, "Queued task should emit a stream notice"

        allow_first_finish.set()
        th1.join(timeout=5.0)
        th2.join(timeout=5.0)
        assert not th1.is_alive() and not th2.is_alive()
        assert second_entered.is_set()
        assert max_active == 1
        assert order == [t1_id, t2_id]

        # --- Part B: exception in primary job releases semaphore so next job runs ---
        broker.start(t3_id, status="generating")
        broker.start(t4_id, status="generating")
        t4_completed = threading.Event()

        def failing_then_succeeding(session, task_id, chat_fn=None, auto_review=False):
            if int(task_id) == t3_id:
                raise RuntimeError("simulated worker crash")
            t4_completed.set()

        monkeypatch.setattr(task_jobs, "run_generate", failing_then_succeeding)

        task_jobs.job_generate(t3_id)
        # Verify t3 was failed cleanly and semaphore was released.
        snap3 = broker.snapshot(t3_id)
        assert snap3 is not None and snap3["terminal"] == "failed"

        th4 = threading.Thread(target=task_jobs.job_generate, args=(t4_id,))
        th4.start()
        th4.join(timeout=5.0)
        assert not th4.is_alive(), "Semaphore leaked after exception in job 3"
        assert t4_completed.is_set()
    finally:
        task_jobs.set_task_max_concurrent(config.TASK_MAX_CONCURRENT)


# ---------------------------------------------------------------------------
# 6. list_tasks O(1) SQL count (<= 12 for 15 tasks) + private prompt parity
# ---------------------------------------------------------------------------


def _setup_auth_and_users(monkeypatch) -> tuple[TestClient, TestClient, int, int, int]:
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    admin = TestClient(create_app())
    resp = admin.post(
        "/api/auth/setup",
        headers=_origin(),
        json={
            "username": "admin_p3",
            "display_name": "Admin P3",
            "password": "CaseGen-Test1!",
        },
    )
    assert resp.status_code == 200, resp.text

    with Session(get_engine()) as session:
        project = session.exec(select(Project)).first()
        assert project is not None and project.id is not None
        project_id = int(project.id)
        alice = User(
            username="alice_p3",
            display_name="Alice",
            password_hash=hash_password("CaseGen-Test1!"),
            role="user",
            is_active=True,
        )
        bob = User(
            username="bob_p3",
            display_name="Bob",
            password_hash=hash_password("CaseGen-Test1!"),
            role="user",
            is_active=True,
        )
        session.add(alice)
        session.add(bob)
        session.commit()
        session.refresh(alice)
        session.refresh(bob)
        alice_id, bob_id = int(alice.id), int(bob.id)

    bob_client = TestClient(create_app())
    login_resp = bob_client.post(
        "/api/auth/login",
        headers=_origin(),
        json={"username": "bob_p3", "password": "CaseGen-Test1!"},
    )
    assert login_resp.status_code == 200, login_resp.text
    return admin, bob_client, project_id, alice_id, bob_id


def test_list_tasks_constant_sql_query_count_and_private_prompt_sanitization(
    tmp_app_data, monkeypatch
):
    _admin, bob_client, project_id, alice_id, bob_id = _setup_auth_and_users(monkeypatch)

    with Session(get_engine()) as session:
        alice_private = PromptTemplate(
            type="generate",
            name="Alice Private Prompt",
            content="private content alice",
            version=1,
            is_active=False,
            visibility="private",
            owner_user_id=alice_id,
        )
        bob_private = PromptTemplate(
            type="generate",
            name="Bob Private Prompt",
            content="private content bob",
            version=1,
            is_active=False,
            visibility="private",
            owner_user_id=bob_id,
        )
        shared_prompt = PromptTemplate(
            type="generate",
            name="Shared Prompt",
            content="shared content",
            version=1,
            is_active=False,
            visibility="shared",
            owner_user_id=alice_id,
        )
        session.add(alice_private)
        session.add(bob_private)
        session.add(shared_prompt)
        session.flush()

        prompt_cycle = [int(alice_private.id), int(bob_private.id), int(shared_prompt.id)]
        created_task_ids: list[int] = []
        for idx in range(15):
            req = Requirement(
                title=f"批量任务-{idx}",
                description=f"验证批量预取 {idx}",
                focus_tags_json=json.dumps(["风控", f"tag-{idx}"]),
            )
            session.add(req)
            session.flush()

            task = GenerationTask(
                requirement_id=int(req.id),
                project_id=project_id,
                status="finalized" if idx % 3 == 0 else "reviewed",
                prompt_template_id=prompt_cycle[idx % 3],
                generation_granularity="standard",
            )
            session.add(task)
            session.flush()
            tid = int(task.id)
            created_task_ids.append(tid)

            # Citations, multi-version drafts, reviews, reference cases, test points
            for c_idx in range(2):
                session.add(
                    TaskCitation(
                        task_id=tid,
                        citation_type="external",
                        title=f"引用-{idx}-{c_idx}",
                        path=f"doc/{idx}_{c_idx}.md",
                        score=0.9,
                        snippet="片段",
                    )
                )

            d1 = CaseDraft(task_id=tid, version=1, content_md=f"## TC-001 - v1-{idx}\n内容")
            d2 = CaseDraft(task_id=tid, version=2, content_md=f"## TC-001 - v2-{idx}\n最新内容")
            session.add(d1)
            session.add(d2)
            session.flush()

            session.add(
                ReviewResult(
                    task_id=tid,
                    draft_id=int(d1.id),
                    score=70,
                    verdict="needs_revision",
                    payload_json=json.dumps({"score": 70, "verdict": "needs_revision"}),
                )
            )
            session.add(
                ReviewResult(
                    task_id=tid,
                    draft_id=int(d2.id),
                    score=88,
                    verdict="pass",
                    payload_json=json.dumps({"score": 88, "verdict": "pass"}),
                )
            )

            session.add(
                TaskReferenceCase(
                    task_id=tid,
                    title_snapshot=f"参考-{idx}",
                    content_md_snapshot="内容",
                    content_hash=f"h-{idx}",
                    source="manual",
                )
            )

            tpc = TaskTestPointCheckpoint(
                task_id=tid,
                attempt=1,
                status="confirmed",
                version=1,
            )
            session.add(tpc)
            session.flush()
            session.add(
                _TestPointRow(
                    task_id=tid,
                    checkpoint_id=int(tpc.id),
                    stable_key="TP-001",
                    title=f"测试点-{idx}",
                    verification_goal="目标",
                    dimension="positive",
                    priority="P0",
                    sort_order=0,
                    is_selected=True,
                    is_excluded=False,
                )
            )

            if idx % 3 == 0:
                task.finalized_draft_id = int(d2.id)
                session.add(task)
                session.add(
                    _TestCaseRow(
                        requirement_id=int(req.id),
                        case_key=f"TC-{idx:03d}",
                        title=f"终版用例-{idx}",
                        content_md="内容",
                        source_task_id=tid,
                        source_draft_id=int(d2.id),
                        source_case_key=f"TC-{idx:03d}",
                    )
                )

        session.commit()
        alice_private_id = int(alice_private.id)
        bob_private_id = int(bob_private.id)
        shared_prompt_id = int(shared_prompt.id)

    engine = get_engine()
    http_sql_statements: list[str] = []

    def _count_http_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            http_sql_statements.append(statement)

    event.listen(engine, "before_cursor_execute", _count_http_sql)
    try:
        list_resp = bob_client.get(f"/api/tasks?project_id={project_id}")
    finally:
        event.remove(engine, "before_cursor_execute", _count_http_sql)

    assert list_resp.status_code == 200, list_resp.text
    items = list_resp.json()
    assert len(items) == 15

    # Direct list_tasks SQL count (excluding auth/project middleware lookups)
    # must stay constant O(1) <= 12 even with 15 richly populated tasks.
    handler_sql_statements: list[str] = []

    def _count_handler_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            handler_sql_statements.append(statement)

    event.listen(engine, "before_cursor_execute", _count_handler_sql)
    try:
        with Session(engine) as session:
            bob_row = session.get(User, bob_id)
            assert bob_row is not None
            direct_items = tasks_api._batch_to_task_out(
                session,
                list(
                    session.exec(
                        select(GenerationTask)
                        .where(GenerationTask.project_id == project_id)
                        .order_by(GenerationTask.id.desc())
                    ).all()
                ),
                user=bob_row,
            )
    finally:
        event.remove(engine, "before_cursor_execute", _count_handler_sql)

    assert len(direct_items) == 15
    assert len(handler_sql_statements) <= 12, (
        f"Expected O(1) list_tasks SQL queries (<= 12) for 15 tasks, got {len(handler_sql_statements)}"
    )
    # Even including auth_sessions + users + projects middleware SELECTs, total HTTP queries stay O(1)
    assert len(http_sql_statements) <= 15, (
        f"Expected O(1) HTTP SQL queries for 15 tasks, got {len(http_sql_statements)}"
    )

    # Verify private prompt sanitization matches single-task GET /api/tasks/{id}
    item_by_id = {int(item["id"]): item for item in items}
    for idx, tid in enumerate(created_task_ids):
        single_resp = bob_client.get(f"/api/tasks/{tid}?project_id={project_id}")
        assert single_resp.status_code == 200, single_resp.text
        single = single_resp.json()
        listed = item_by_id[tid]

        assert listed == single
        if idx % 3 == 0:
            # Alice's private prompt must be masked for Bob
            assert listed["prompt_template_id"] is None
            assert single["prompt_template_id"] is None
            assert listed["prompt_template_id"] != alice_private_id
        elif idx % 3 == 1:
            # Bob's own private prompt stays visible to Bob
            assert listed["prompt_template_id"] == bob_private_id
        else:
            # Shared prompt stays visible to Bob
            assert listed["prompt_template_id"] == shared_prompt_id


# ---------------------------------------------------------------------------
# 7. stream_broker & shared build_sse_streaming_response across task & agent SSE
# ---------------------------------------------------------------------------


def test_stream_broker_and_sse_endpoints_share_build_sse_streaming_response(
    tmp_app_data, monkeypatch
):
    # --- Part A: direct BaseStreamBroker + build_sse_streaming_response unit tests ---
    broker: BaseStreamBroker[str] = BaseStreamBroker(max_events_per_task=8)
    broker.start("k1", status="running", message="启动")
    for idx in range(12):
        broker.delta("k1", f"chunk-{idx}-")
    # Late subscriber with sequence=0 experiences ring-buffer overflow -> snapshot resync
    overflow_poll = broker.poll_after("k1", 0)
    assert overflow_poll.kind == "snapshot"
    assert overflow_poll.snapshot is not None
    assert "chunk-11-" in overflow_poll.snapshot["text"]

    # Stream emits snapshot -> delta -> completed and closes cleanly
    broker2: BaseStreamBroker[str] = BaseStreamBroker()
    broker2.start("live", status="running", message="运行中")

    async def _collect_live() -> tuple[dict[str, str], list[dict[str, Any]]]:
        response = build_sse_streaming_response(
            broker2,
            "live",
            poll_interval_sec=0.01,
            heartbeat_sec=60.0,
            headers={"X-Custom-Stream": "phase3"},
        )

        async def _producer():
            await asyncio.sleep(0.02)
            broker2.delta("live", "hello ")
            broker2.delta("live", "world")
            broker2.complete("live", text="hello world", status="done", message="完成")

        producer_task = asyncio.create_task(_producer())
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
        await producer_task
        return dict(response.headers), _parse_sse_frames("".join(chunks))

    headers, frames = asyncio.run(_collect_live())
    for k, v in DEFAULT_SSE_HEADERS.items():
        assert headers[k.lower()] == v
    assert headers["x-custom-stream"] == "phase3"
    assert [f["event"] for f in frames] == ["snapshot", "delta", "delta", "completed"]
    assert frames[-1]["data"]["status"] == "done"
    assert frames[-1]["data"]["text"] == "hello world"

    # Eviction / discard terminates stream cleanly without hanging
    broker3: BaseStreamBroker[str] = BaseStreamBroker()
    broker3.start("evict-me", status="running")

    async def _collect_evicted() -> list[dict[str, Any]]:
        response = build_sse_streaming_response(
            broker3,
            "evict-me",
            poll_interval_sec=0.01,
            heartbeat_sec=60.0,
        )

        async def _evict():
            await asyncio.sleep(0.02)
            broker3.discard("evict-me")

        task = asyncio.create_task(_evict())
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
        await task
        return _parse_sse_frames("".join(chunks))

    evicted_frames = asyncio.run(_collect_evicted())
    assert [f["event"] for f in evicted_frames] == ["snapshot"]

    # --- Part B: both /api/tasks/{id}/stream and /api/agent/conversations/{cid}/stream
    #             delegate to build_sse_streaming_response ---
    helper_calls: list[tuple[str, Any]] = []
    real_builder = build_sse_streaming_response

    def _spy_builder(source_broker, key, **kwargs):
        helper_calls.append((type(source_broker).__name__, key))
        return real_builder(source_broker, key, **kwargs)

    task_broker = TaskStreamBroker()
    agent_broker = AgentStreamBroker()
    monkeypatch.setattr(tasks_api, "task_stream", task_broker)
    monkeypatch.setattr(tasks_api, "build_sse_streaming_response", _spy_builder)
    monkeypatch.setattr(tasks_api, "_STREAM_POLL_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(agent_chat_api, "agent_stream", agent_broker)
    monkeypatch.setattr(agent_chat_api, "build_sse_streaming_response", _spy_builder)
    monkeypatch.setattr(agent_chat_api, "_STREAM_POLL_INTERVAL_SEC", 0.01)

    client = TestClient(create_app())
    with Session(get_engine()) as session:
        project = session.exec(select(Project)).first()
        assert project is not None and project.id is not None
        project_id = int(project.id)

        req = Requirement(title="SSE 回归", description="验证共用流响应")
        session.add(req)
        session.flush()
        task = GenerationTask(
            requirement_id=int(req.id),
            project_id=project_id,
            status="generated",
        )
        session.add(task)
        session.flush()
        task_id = int(task.id)

        conv = AgentConversation(
            id="conv-phase3-sse",
            project_id=str(project_id),
            title="Agent SSE 回归",
            status="active",
        )
        session.add(conv)
        session.commit()

    task_broker.start(task_id, status="generating", message="生成中")
    task_broker.delta(task_id, "## TC-001 - 示例")
    task_broker.complete(task_id, text="## TC-001 - 示例", status="generated", message="完成")

    task_resp = client.get(f"/api/tasks/{task_id}/stream")
    assert task_resp.status_code == 200
    assert task_resp.headers["cache-control"] == "no-store"
    assert task_resp.headers["x-accel-buffering"] == "no"
    task_frames = _parse_sse_frames(task_resp.text)
    assert task_frames[0]["event"] == "snapshot"
    assert task_frames[0]["data"]["terminal"] == "completed"
    assert task_frames[0]["data"]["text"] == "## TC-001 - 示例"

    agent_broker.start("conv-phase3-sse", status="busy", message="思考中")
    agent_broker.reasoning("conv-phase3-sse", text="先分析需求")
    agent_broker.fail(
        "conv-phase3-sse",
        message="用户取消",
        status="active",
        category="cancelled",
    )

    agent_resp = client.get(
        f"/api/agent/conversations/conv-phase3-sse/stream?project_id={project_id}"
    )
    assert agent_resp.status_code == 200
    assert agent_resp.headers["cache-control"] == "no-store"
    assert agent_resp.headers["x-accel-buffering"] == "no"
    agent_frames = _parse_sse_frames(agent_resp.text)
    assert agent_frames[0]["event"] == "snapshot"
    assert agent_frames[0]["data"]["terminal"] == "failed"
    assert agent_frames[0]["data"]["category"] == "cancelled"
    assert agent_frames[0]["data"]["reasoning"] == "先分析需求"

    assert ("TaskStreamBroker", task_id) in helper_calls
    assert ("AgentStreamBroker", "conv-phase3-sse") in helper_calls


# ---------------------------------------------------------------------------
# 8. Review-fix regressions: manual stop x lean fallback retry & semaphore queue
# ---------------------------------------------------------------------------


def test_single_point_llm_error_after_manual_stop_does_not_retry_or_overwrite_error(
    tmp_app_data, monkeypatch
):
    client = TestClient(create_app())
    model_id = _create_model(client, name="stop-retry-model")

    import app.services.pipeline.generation as generation_mod
    import app.services.pipeline.orchestrator as orchestrator_mod

    broker = TaskStreamBroker()
    monkeypatch.setattr(tasks_api, "task_stream", broker)
    monkeypatch.setattr(generation_mod, "task_stream", broker)
    monkeypatch.setattr(orchestrator_mod, "task_stream", broker)

    with Session(get_engine()) as session:
        req = Requirement(title="手动停止不重试", description="验证主生成抛错同时已停止不走精简重试")
        session.add(req)
        session.flush()
        task = GenerationTask(
            requirement_id=int(req.id),
            status="generating",
            model_id=model_id,
        )
        session.add(task)
        session.flush()
        task_id = int(task.id)

        citation = TaskCitation(
            task_id=task_id,
            citation_type="external",
            title="风控规则",
            path="rules/stop_retry.md",
            snippet="风控校验规则片段",
            content_excerpt="风控校验规则片段",
            score=0.95,
        )
        session.add(citation)
        session.flush()

        rc = TaskRetrievalCheckpoint(
            task_id=task_id,
            attempt=1,
            status="confirmed",
            query="风控校验",
            retrieval_json=json.dumps(
                {
                    "context": {
                        "citations": [
                            {
                                "task_citation_id": citation.id,
                                "title": citation.title,
                                "path": citation.path,
                            }
                        ],
                        "wiki_hits": [],
                        "source_hits": [],
                        "external_hits": [],
                    }
                },
                ensure_ascii=False,
            ),
            selected_citation_ids_json=json.dumps([citation.id]),
        )
        session.add(rc)
        session.flush()

        tpc = TaskTestPointCheckpoint(
            task_id=task_id,
            retrieval_checkpoint_id=int(rc.id),
            attempt=1,
            status="confirmed",
            version=1,
        )
        session.add(tpc)
        session.flush()

        for idx in range(2):
            tp = _TestPointRow(
                task_id=task_id,
                checkpoint_id=int(tpc.id),
                stable_key=f"TP-00{idx + 1}",
                title=f"测试点-{idx + 1}",
                verification_goal="验证目标",
                dimension="positive",
                priority="P0",
                sort_order=idx,
                is_selected=True,
                is_excluded=False,
            )
            session.add(tp)
            session.flush()
            session.add(
                _TestPointCitationRow(test_point_id=int(tp.id), citation_id=int(citation.id))
            )
        session.commit()

        broker.start(task_id, status="generating", message="开始生成")
        call_count = 0

        def stop_mid_call_and_raise(messages, model=None):
            nonlocal call_count
            call_count += 1
            # User clicks manual stop while the primary LLM request is in flight.
            stop_resp = client.post(f"/api/tasks/{task_id}/stop")
            assert stop_resp.status_code == 200, stop_resp.text
            raise LLMError("upstream aborted after client stop")

        result = run_generate(session, task_id, chat_fn=stop_mid_call_and_raise)

        assert call_count == 1, "Stopped task must not invoke lean-fallback retry LLM call"
        assert result.status == "failed"
        assert result.error_message == "已手动停止生成"

        # Verify DB durability, no draft written, and no lean-fallback retry notice sent.
        session.expire_all()
        persisted = session.get(GenerationTask, task_id)
        assert persisted is not None
        assert persisted.status == "failed"
        assert persisted.error_message == "已手动停止生成"
        assert session.exec(select(CaseDraft).where(CaseDraft.task_id == task_id)).all() == []

        notices = [ev for ev in broker.events_after(task_id, 0) if ev.event == "notice"]
        assert not any("精简上下文重试" in str(ev.payload.get("message", "")) for ev in notices)

        events = session.exec(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id.asc())
        ).all()
        assert any("检测到手动停止，中止后续生成" in ev.message for ev in events)
        assert not any("精简上下文重试" in ev.message for ev in events)


def test_queued_task_stopped_before_slot_acquired_does_not_revive(
    tmp_app_data, monkeypatch
):
    client = TestClient(create_app())
    broker = TaskStreamBroker()
    monkeypatch.setattr(task_jobs, "task_stream", broker)
    monkeypatch.setattr(tasks_api, "task_stream", broker)
    monkeypatch.setattr(task_jobs.logger, "exception", lambda *args, **kwargs: None)

    with Session(get_engine()) as session:
        req1 = Requirement(title="slot-holder", description="占槽任务")
        req2 = Requirement(title="queued-stopped", description="排队期间手动停止任务")
        session.add(req1)
        session.add(req2)
        session.flush()

        t1 = GenerationTask(requirement_id=int(req1.id), status="generating")
        t2 = GenerationTask(requirement_id=int(req2.id), status="generating")
        session.add(t1)
        session.add(t2)
        session.flush()
        t1_id, t2_id = int(t1.id), int(t2.id)

        cp2 = TaskTestPointCheckpoint(
            task_id=t2_id,
            attempt=1,
            status="confirmed",
            version=1,
            resume_claim_token="claim-token-t2",
            resume_status="claimed",
        )
        session.add(cp2)
        session.commit()
        cp2_id = int(cp2.id)

    try:
        task_jobs.set_task_max_concurrent(1)
        broker.start(t1_id, status="generating")
        broker.start(t2_id, status="generating")

        first_entered = threading.Event()
        allow_first_finish = threading.Event()
        executed_task_ids: list[int] = []

        def fake_run_generate(session, task_id, chat_fn=None, auto_review=False):
            executed_task_ids.append(int(task_id))
            if int(task_id) == t1_id:
                first_entered.set()
                assert allow_first_finish.wait(timeout=5.0)

        monkeypatch.setattr(task_jobs, "run_generate", fake_run_generate)

        th1 = threading.Thread(target=task_jobs.job_generate, args=(t1_id,))
        th2 = threading.Thread(
            target=task_jobs.job_generate,
            args=(t2_id, False, cp2_id, "claim-token-t2", "test_points"),
        )
        th1.start()
        assert first_entered.wait(timeout=5.0)

        th2.start()
        # Wait until task 2 is confirmed queued behind the concurrency semaphore.
        deadline = time.monotonic() + 2.0
        saw_queue_notice = False
        while time.monotonic() < deadline:
            notices = [ev for ev in broker.events_after(t2_id, 0) if ev.event == "notice"]
            if any("排队等待执行槽位" in str(ev.payload.get("message", "")) for ev in notices):
                saw_queue_notice = True
                break
            time.sleep(0.02)
        assert saw_queue_notice

        # Stop task 2 while it is waiting for the concurrency slot.
        stop_resp = client.post(f"/api/tasks/{t2_id}/stop")
        assert stop_resp.status_code == 200, stop_resp.text

        # Release slot from task 1; task 2 wakes up and must exit without reviving.
        allow_first_finish.set()
        th1.join(timeout=5.0)
        th2.join(timeout=5.0)
        assert not th1.is_alive() and not th2.is_alive()

        assert executed_task_ids == [t1_id], "Stopped queued task must never enter run_generate"
        with Session(get_engine()) as session:
            t2_row = session.get(GenerationTask, t2_id)
            assert t2_row is not None
            assert t2_row.status == "failed"
            assert t2_row.error_message == "已手动停止生成"

            cp2_row = session.get(TaskTestPointCheckpoint, cp2_id)
            assert cp2_row is not None
            assert cp2_row.resume_status == "stopped"
            assert cp2_row.resume_started_at is None
    finally:
        task_jobs.set_task_max_concurrent(config.TASK_MAX_CONCURRENT)
