"""Background runners for long LLM pipeline steps.

Each job opens its own DB session so FastAPI request sessions can close
immediately after scheduling.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
import secrets
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from sqlalchemy import or_, update
from sqlmodel import Session, select

from app import config
from app.db import get_engine
from app.models.entities import (
    CaseDraft,
    GenerationTask,
    ReviewResult,
    TaskRetrievalCheckpoint,
    TaskTestPointCheckpoint,
)
from app.services.task_events import append_event
from app.services.task_locks import task_locks
from app.services.task_pipeline import (
    run_generate,
    run_optimize_prompt,
    run_regenerate,
    run_review,
)
from app.services.task_state import transition
from app.services.task_stream import task_stream

logger = logging.getLogger(__name__)

# Process-wide concurrency limiter shared across background LLM jobs.
_task_semaphore_lock = threading.Lock()
_task_max_concurrent: int = config.TASK_MAX_CONCURRENT
_task_concurrency_semaphore = threading.BoundedSemaphore(_task_max_concurrent)


def get_task_semaphore() -> threading.BoundedSemaphore:
    """Return the active process-wide concurrency semaphore for background jobs."""
    with _task_semaphore_lock:
        return _task_concurrency_semaphore


def set_task_max_concurrent(limit: int) -> threading.BoundedSemaphore:
    """Replace the global task concurrency semaphore (intended for tests/config)."""
    global _task_max_concurrent, _task_concurrency_semaphore
    safe_limit = max(1, int(limit))
    with _task_semaphore_lock:
        _task_max_concurrent = safe_limit
        _task_concurrency_semaphore = threading.BoundedSemaphore(safe_limit)
        return _task_concurrency_semaphore


@contextmanager
def _acquire_task_slot(task_id: int, *, notify_stream: bool = False) -> Iterator[None]:
    """Acquire one global background execution slot and always release on exit."""
    sem = get_task_semaphore()
    if not sem.acquire(blocking=False):
        if notify_stream:
            snap = task_stream.snapshot(task_id)
            if snap is not None and snap.get("terminal") is None:
                task_stream.notice(
                    task_id,
                    message="当前并发任务较多，正在排队等待执行槽位…",
                )
        sem.acquire()
    try:
        yield
    finally:
        sem.release()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class _JobClaim:
    """Identity captured while a background job still owns its task.

    ``created_at`` never changes across a task lifecycle, so it reliably
    distinguishes a replacement task that reused the same integer id.  The
    broker ``stream_id`` additionally pins the live preview owned by this job
    so a late failure handler can never terminate a replacement task's stream.
    """

    claimed: bool
    expected_stream_id: int | None = None
    launch_created_at: datetime | None = None
    checkpoint_id: int | None = None
    claim_token: str | None = None
    checkpoint_kind: str = "retrieval"


def _checkpoint_model(kind: str):
    return TaskTestPointCheckpoint if kind == "test_points" else TaskRetrievalCheckpoint


def _claim_job(
    task_id: int,
    *,
    expected_statuses: frozenset[str],
    require_stream: bool,
    checkpoint_id: int | None = None,
    claim_token: str | None = None,
    checkpoint_kind: str = "retrieval",
) -> _JobClaim:
    """Atomically verify a background job still owns its task before running."""
    with task_locks.hold(task_id):
        snapshot = task_stream.snapshot(task_id)
        expected_stream_id = (
            int(snapshot["stream_id"]) if snapshot is not None else None
        )
        if require_stream:
            if expected_stream_id is None:
                return _JobClaim(False)
            if snapshot is not None and snapshot.get("terminal") is not None:
                return _JobClaim(False)
        with Session(get_engine()) as session:
            task = session.get(GenerationTask, task_id)
            if task is None or task.status not in expected_statuses:
                return _JobClaim(False)
            if checkpoint_id is not None:
                checkpoint = session.get(_checkpoint_model(checkpoint_kind), checkpoint_id)
                if checkpoint is None or checkpoint.task_id != task_id:
                    return _JobClaim(False)
                if claim_token is None or checkpoint.resume_claim_token != claim_token:
                    return _JobClaim(False)
                if checkpoint.resume_status not in (None, "claimed"):
                    return _JobClaim(False)
            launch_created_at = task.created_at
        return _JobClaim(
            True,
            expected_stream_id,
            launch_created_at,
            checkpoint_id,
            claim_token,
            checkpoint_kind,
        )


def _verify_claim_after_slot(
    task_id: int,
    claim: _JobClaim,
    *,
    expected_statuses: frozenset[str],
    require_stream: bool,
) -> bool:
    """Re-verify task/stream ownership after waking from the concurrency semaphore.

    A queued job may have been manually stopped, replaced, or moved to another
    status while waiting for an execution slot.
    """
    with task_locks.hold(task_id):
        if require_stream:
            snapshot = task_stream.snapshot(task_id)
            if snapshot is None or snapshot.get("terminal") is not None:
                return False
            if (
                claim.expected_stream_id is not None
                and int(snapshot.get("stream_id") or 0) != claim.expected_stream_id
            ):
                return False
        with Session(get_engine()) as session:
            task = session.get(GenerationTask, task_id)
            if task is None or task.status not in expected_statuses:
                return False
            if (
                claim.launch_created_at is not None
                and task.created_at != claim.launch_created_at
            ):
                return False
            if claim.checkpoint_id is not None:
                checkpoint = session.get(
                    _checkpoint_model(claim.checkpoint_kind), claim.checkpoint_id
                )
                if checkpoint is None or checkpoint.task_id != task_id:
                    return False
                if (
                    claim.claim_token is None
                    or checkpoint.resume_claim_token != claim.claim_token
                ):
                    return False
                if checkpoint.resume_status not in (None, "claimed"):
                    return False
    return True


def _consume_resume_claim(claim: _JobClaim) -> bool:
    """Atomically consume a durable lease so the same token runs once."""
    if claim.checkpoint_id is None:
        return True
    now = _utcnow()
    with Session(get_engine()) as session:
        result = session.exec(
            update(_checkpoint_model(claim.checkpoint_kind))
            .where(_checkpoint_model(claim.checkpoint_kind).id == claim.checkpoint_id)
            .where(_checkpoint_model(claim.checkpoint_kind).status == "confirmed")
            .where(_checkpoint_model(claim.checkpoint_kind).resume_claim_token == claim.claim_token)
            .where(_checkpoint_model(claim.checkpoint_kind).resume_started_at.is_(None))
            .where(
                or_(
                    _checkpoint_model(claim.checkpoint_kind).resume_status.is_(None),
                    _checkpoint_model(claim.checkpoint_kind).resume_status == "claimed",
                )
            )
            .values(resume_started_at=now, resume_status="running", updated_at=now)
        )
        if result.rowcount != 1:
            session.rollback()
            return False
        session.commit()
    return True


def _finish_resume_claim(claim: _JobClaim, status: str) -> None:
    if claim.checkpoint_id is None:
        return
    with Session(get_engine()) as session:
        session.exec(
            update(_checkpoint_model(claim.checkpoint_kind))
            .where(_checkpoint_model(claim.checkpoint_kind).id == claim.checkpoint_id)
            .where(_checkpoint_model(claim.checkpoint_kind).resume_claim_token == claim.claim_token)
            .where(_checkpoint_model(claim.checkpoint_kind).resume_status == "running")
            .values(resume_status=status, updated_at=_utcnow())
        )
        session.commit()


def _fail_if_stuck(session: Session, task_id: int, message: str) -> bool:
    task = session.get(GenerationTask, task_id)
    if task is None:
        return False
    if task.status in (
        "retrieving",
        "generating_test_points",
        "generating",
        "reviewing",
        "optimizing",
        "regenerating",
    ):
        task.status = "failed"
        task.error_message = message[:2000]
        session.add(task)
        session.commit()
        return True
    return False


def _heartbeat_resume_claim(
    checkpoint_id: int,
    claim_token: str,
    stop_event: threading.Event,
    interval: float = 30.0,
    checkpoint_kind: str = "retrieval",
) -> None:
    """Refresh a running lease until the worker finishes or loses ownership."""
    while not stop_event.wait(interval):
        now = _utcnow()
        with Session(get_engine()) as session:
            result = session.exec(
                update(_checkpoint_model(checkpoint_kind))
                .where(_checkpoint_model(checkpoint_kind).id == checkpoint_id)
                .where(_checkpoint_model(checkpoint_kind).resume_claim_token == claim_token)
                .where(_checkpoint_model(checkpoint_kind).resume_status == "running")
                .values(resume_claimed_at=now, updated_at=now)
            )
            session.commit()
            if result.rowcount != 1:
                return


def _claim_auto_review(task_id: int, launch_created_at: datetime | None) -> bool:
    """Atomically claim generated -> reviewing before running auto-review."""
    with task_locks.hold(task_id):
        with Session(get_engine()) as session:
            task = session.get(GenerationTask, task_id)
            if task is None or task.status != "generated":
                return False
            if (
                launch_created_at is not None
                and task.created_at != launch_created_at
            ):
                return False
            task.status = transition(task.status, "reviewing")
            task.error_message = None
            task.updated_at = _utcnow()
            session.add(task)
            append_event(session, task_id, "review", "后台开始自动评审")
            session.commit()
            return True


def _mark_job_failed(
    task_id: int,
    *,
    durable_message: str,
    stream_message: str | None = None,
    expected_stream_id: int | None = None,
    launch_created_at: datetime | None = None,
) -> None:
    """Reload and fail a busy task under its lifecycle lock."""
    with task_locks.hold(task_id):
        with Session(get_engine()) as session:
            task = session.get(GenerationTask, task_id)
            if task is None:
                return
            if (
                launch_created_at is not None
                and task.created_at != launch_created_at
            ):
                return
            marked = _fail_if_stuck(session, task_id, durable_message)
        if marked and stream_message is not None and expected_stream_id is not None:
            task_stream.fail_if_active(
                task_id,
                message=stream_message,
                expected_stream_id=expected_stream_id,
            )


def job_generate(
    task_id: int,
    auto_review: bool = False,
    checkpoint_id: int | None = None,
    claim_token: str | None = None,
    checkpoint_kind: str = "retrieval",
) -> None:
    expected_statuses = frozenset({"retrieving", "generating_test_points", "generating"})
    claim = _claim_job(
        task_id,
        expected_statuses=expected_statuses,
        require_stream=True,
        checkpoint_id=checkpoint_id,
        claim_token=claim_token,
        checkpoint_kind=checkpoint_kind,
    )
    if not claim.claimed:
        return
    with _acquire_task_slot(task_id, notify_stream=True):
        if not _verify_claim_after_slot(
            task_id,
            claim,
            expected_statuses=expected_statuses,
            require_stream=True,
        ):
            return
        if not _consume_resume_claim(claim):
            return
        heartbeat_stop = threading.Event()
        if claim.checkpoint_id is not None and claim.claim_token is not None:
            heartbeat_thread = threading.Thread(
                target=_heartbeat_resume_claim,
                args=(
                    claim.checkpoint_id,
                    claim.claim_token,
                    heartbeat_stop,
                    30.0,
                    claim.checkpoint_kind,
                ),
                daemon=True,
            )
            heartbeat_thread.start()
        try:
            with Session(get_engine()) as session:
                run_generate(session, task_id, chat_fn=None, auto_review=auto_review)
            if auto_review and _claim_auto_review(task_id, claim.launch_created_at):
                with Session(get_engine()) as review_session:
                    run_review(review_session, task_id, chat_fn=None)
            _finish_resume_claim(claim, "completed")
        except Exception as exc:
            logger.exception("background generate failed task_id=%s", task_id)
            try:
                _mark_job_failed(
                    task_id,
                    durable_message=(
                        f"后台生成失败: {exc}\n{traceback.format_exc()[-500:]}"
                    ),
                    stream_message="后台生成失败，请稍后重试",
                    expected_stream_id=claim.expected_stream_id,
                    launch_created_at=claim.launch_created_at,
                )
            except Exception:
                logger.exception("failed to mark task failed task_id=%s", task_id)
        finally:
            heartbeat_stop.set()
            _finish_resume_claim(
                claim, "failed" if claim.checkpoint_id is not None else "completed"
            )


def recover_generation_jobs() -> list[int]:
    """Recover all interrupted busy task states on process startup.

    Covers:
    - ``generating_test_points`` / ``generating``: durable checkpoint lease recovery
    - ``retrieving``: reopen live stream and reschedule ``job_generate``
    - ``regenerating``: reopen live stream and reschedule ``job_regenerate``
    - ``reviewing``: reschedule ``job_review`` if a draft exists, otherwise fail safely
    - ``optimizing``: reschedule ``job_optimize_prompt`` if a review exists, otherwise revert to ``reviewed``
    """
    with Session(get_engine()) as session:
        checkpoint_tasks = session.exec(
            select(GenerationTask)
            .where(GenerationTask.status.in_({"generating_test_points", "generating"}))
            .order_by(GenerationTask.id.asc())
        ).all()
        claimed_rows: list[tuple[int, int, bool, str, str]] = []
        now = _utcnow()
        for task in checkpoint_tasks:
            task_id = int(task.id)
            kind = "retrieval" if task.status == "generating_test_points" else "test_points"
            model = _checkpoint_model(kind)
            statement = (
                select(model)
                .where(model.task_id == task_id)
                .where(model.status == "confirmed")
                .order_by(model.attempt.desc())
            )
            current = session.exec(statement).first()
            if current is None and task.status == "generating":
                kind = "retrieval"
                model = TaskRetrievalCheckpoint
                current = session.exec(
                    select(model)
                    .where(model.task_id == task_id)
                    .where(model.status == "confirmed")
                    .order_by(model.attempt.desc())
                ).first()
                if current is not None:
                    task.status = transition(task.status, "generating_test_points")
                    task.updated_at = now
                    session.add(task)
                    append_event(
                        session,
                        task_id,
                        "recovery",
                        "兼容恢复：旧生成任务迁移到测试点生成阶段",
                        detail={"retrieval_checkpoint_id": current.id},
                    )
            if current is None:
                continue
            if task.status == "generating" and session.exec(
                select(CaseDraft.id).where(CaseDraft.task_id == task_id)
            ).first() is not None:
                continue
            if (
                current.resume_claim_token is not None
                and current.resume_status in {"claimed", "running"}
                and (
                    current.resume_claimed_at is None
                    or current.resume_claimed_at >= now - timedelta(minutes=5)
                )
            ):
                continue
            token = secrets.token_urlsafe(24)
            result = session.exec(
                update(model)
                .where(model.id == int(current.id))
                .where(model.status == "confirmed")
                .where(
                    or_(
                        model.resume_claim_token.is_(None),
                        model.resume_claimed_at < (now - timedelta(minutes=5)),
                        model.resume_status == "completed",
                    )
                )
                .values(
                    resume_claim_token=token,
                    resume_claimed_at=now,
                    resume_status="claimed",
                    resume_started_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                continue
            claimed_rows.append(
                (task_id, int(current.id), bool(getattr(current, "auto_review", False)), token, kind)
            )

        other_tasks = session.exec(
            select(GenerationTask)
            .where(GenerationTask.status.in_({"retrieving", "regenerating", "reviewing", "optimizing"}))
            .order_by(GenerationTask.id.asc())
        ).all()
        retrieving_ids: list[int] = []
        regenerating_ids: list[int] = []
        reviewing_ids: list[int] = []
        optimizing_ids: list[int] = []

        for task in other_tasks:
            task_id = int(task.id)
            # Re-entry guard: skip tasks that still have an active in-process stream
            # (e.g. duplicate create_app() invocations in the same process).
            live_snap = task_stream.snapshot(task_id)
            if live_snap is not None and live_snap.get("terminal") is None:
                continue
            if task.status == "retrieving":
                append_event(
                    session,
                    task_id,
                    "recovery",
                    "进程重启恢复：重新执行知识检索",
                )
                retrieving_ids.append(task_id)
            elif task.status == "regenerating":
                append_event(
                    session,
                    task_id,
                    "recovery",
                    "进程重启恢复：重新执行再生成任务",
                )
                regenerating_ids.append(task_id)
            elif task.status == "reviewing":
                has_draft = (
                    session.exec(
                        select(CaseDraft.id).where(CaseDraft.task_id == task_id)
                    ).first()
                    is not None
                )
                if has_draft:
                    append_event(
                        session,
                        task_id,
                        "recovery",
                        "进程重启恢复：重新执行用例评审",
                    )
                    reviewing_ids.append(task_id)
                else:
                    task.status = transition(task.status, "failed")
                    task.error_message = "进程重启导致评审中断，请重试"
                    task.updated_at = now
                    session.add(task)
                    append_event(
                        session,
                        task_id,
                        "recovery",
                        "进程重启导致评审中断且缺少草稿，已标记失败",
                    )
            elif task.status == "optimizing":
                has_review = (
                    session.exec(
                        select(ReviewResult.id).where(ReviewResult.task_id == task_id)
                    ).first()
                    is not None
                )
                if has_review:
                    append_event(
                        session,
                        task_id,
                        "recovery",
                        "进程重启恢复：重新执行提示词优化",
                    )
                    optimizing_ids.append(task_id)
                else:
                    task.status = transition(task.status, "reviewed")
                    task.error_message = None
                    task.updated_at = now
                    session.add(task)
                    append_event(
                        session,
                        task_id,
                        "recovery",
                        "进程重启恢复：缺少评审记录，已安全回退至 reviewed",
                    )

        session.commit()

    ids: list[int] = []
    for task_id, checkpoint_id, auto_review, token, kind in claimed_rows:
        status = "generating_test_points" if kind == "retrieval" else "generating"
        task_stream.start(task_id, status=status, message="恢复中断的生成任务")
        threading.Thread(
            target=job_generate,
            args=(task_id, auto_review, checkpoint_id, token, kind),
            daemon=True,
        ).start()
        ids.append(task_id)

    for task_id in retrieving_ids:
        task_stream.start(task_id, status="retrieving", message="恢复中断的检索任务")
        threading.Thread(
            target=job_generate,
            args=(task_id, False),
            daemon=True,
        ).start()
        ids.append(task_id)

    for task_id in regenerating_ids:
        task_stream.start(task_id, status="regenerating", message="恢复中断的再生成任务")
        threading.Thread(
            target=job_regenerate,
            args=(task_id,),
            daemon=True,
        ).start()
        ids.append(task_id)

    for task_id in reviewing_ids:
        threading.Thread(
            target=job_review,
            args=(task_id,),
            daemon=True,
        ).start()
        ids.append(task_id)

    for task_id in optimizing_ids:
        threading.Thread(
            target=job_optimize_prompt,
            args=(task_id,),
            daemon=True,
        ).start()
        ids.append(task_id)

    return ids


def job_review(task_id: int) -> None:
    expected_statuses = frozenset({"reviewing"})
    claim = _claim_job(
        task_id,
        expected_statuses=expected_statuses,
        require_stream=False,
    )
    if not claim.claimed:
        return
    try:
        with _acquire_task_slot(task_id, notify_stream=False):
            if not _verify_claim_after_slot(
                task_id,
                claim,
                expected_statuses=expected_statuses,
                require_stream=False,
            ):
                return
            with Session(get_engine()) as session:
                run_review(session, task_id, chat_fn=None)
    except Exception as exc:
        logger.exception("background review failed task_id=%s", task_id)
        try:
            _mark_job_failed(
                task_id,
                durable_message=f"后台评审失败: {exc}",
                expected_stream_id=claim.expected_stream_id,
                launch_created_at=claim.launch_created_at,
            )
        except Exception:
            logger.exception("failed to mark task failed task_id=%s", task_id)


def job_regenerate(task_id: int) -> None:
    expected_statuses = frozenset({"regenerating"})
    claim = _claim_job(
        task_id,
        expected_statuses=expected_statuses,
        require_stream=True,
    )
    if not claim.claimed:
        return
    try:
        with _acquire_task_slot(task_id, notify_stream=True):
            if not _verify_claim_after_slot(
                task_id,
                claim,
                expected_statuses=expected_statuses,
                require_stream=True,
            ):
                return
            with Session(get_engine()) as session:
                run_regenerate(session, task_id, chat_fn=None)
    except Exception as exc:
        logger.exception("background regenerate failed task_id=%s", task_id)
        try:
            _mark_job_failed(
                task_id,
                durable_message=f"后台再生成失败: {exc}",
                stream_message="后台再生成失败，请稍后重试",
                expected_stream_id=claim.expected_stream_id,
                launch_created_at=claim.launch_created_at,
            )
        except Exception:
            logger.exception("failed to mark task failed task_id=%s", task_id)


def job_optimize_prompt(task_id: int) -> None:
    expected_statuses = frozenset({"optimizing"})
    claim = _claim_job(
        task_id,
        expected_statuses=expected_statuses,
        require_stream=False,
    )
    if not claim.claimed:
        return
    try:
        with _acquire_task_slot(task_id, notify_stream=False):
            if not _verify_claim_after_slot(
                task_id,
                claim,
                expected_statuses=expected_statuses,
                require_stream=False,
            ):
                return
            with Session(get_engine()) as session:
                run_optimize_prompt(session, task_id, chat_fn=None)
    except Exception as exc:
        logger.exception("background optimize failed task_id=%s", task_id)
        try:
            _mark_job_failed(
                task_id,
                durable_message=f"后台优化提示词失败: {exc}",
                expected_stream_id=claim.expected_stream_id,
                launch_created_at=claim.launch_created_at,
            )
        except Exception:
            logger.exception("failed to mark task failed task_id=%s", task_id)


def mark_task_status(
    session: Session,
    task_id: int,
    status: str,
    *,
    error_message: Optional[str] = None,
) -> Optional[GenerationTask]:
    task = session.get(GenerationTask, task_id)
    if task is None:
        return None
    task.status = status
    if error_message is not None:
        task.error_message = error_message
    session.add(task)
    session.commit()
    session.refresh(task)
    return task
