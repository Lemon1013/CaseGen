"""Background runners for long LLM pipeline steps.

Each job opens its own DB session so FastAPI request sessions can close
immediately after scheduling.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session

from app.db import get_engine
from app.models.entities import GenerationTask
from app.services.task_events import append_event
from app.services.task_locks import task_locks
from app.services.task_pipeline import (
    run_generate,
    run_optimize_prompt,
    run_regenerate,
    run_review,
)
from app.services.task_stream import task_stream
from app.services.task_state import transition

logger = logging.getLogger(__name__)


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


def _claim_job(
    task_id: int,
    *,
    expected_statuses: frozenset[str],
    require_stream: bool,
) -> _JobClaim:
    """Atomically verify a background job still owns its task before running.

    Runs under the per-task lifecycle lock so a delete/recreate, a manual
    status change, or a second scheduled job cannot interleave with the
    identity check.  Long pipeline work happens after this claim is released;
    durable writes stay protected by the pipeline state machine, and failure
    handling revalidates ``created_at`` plus ``stream_id``.
    """
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
            launch_created_at = task.created_at
        return _JobClaim(True, expected_stream_id, launch_created_at)


def _fail_if_stuck(session: Session, task_id: int, message: str) -> bool:
    task = session.get(GenerationTask, task_id)
    if task is None:
        return False
    if task.status in (
        "retrieving",
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


def _claim_auto_review(task_id: int, launch_created_at: datetime | None) -> bool:
    """Atomically claim generated -> reviewing before running auto-review.

    A replacement task that reused the same integer id has a different
    ``created_at`` and therefore never satisfies this claim.
    """
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
    """Reload and fail a busy task under its lifecycle lock.

    The captured broker identity and task ``created_at`` prevent a late
    exception handler from marking or terminating a replacement task that
    reused the same integer id. Missing/deleted rows leave the broker
    untouched.
    """
    with task_locks.hold(task_id):
        with Session(get_engine()) as session:
            task = session.get(GenerationTask, task_id)
            if task is None:
                return
            if (
                launch_created_at is not None
                and task.created_at != launch_created_at
            ):
                # The task was deleted and its id reused; this job no longer
                # owns anything and must not touch the replacement row or its
                # live stream.
                return
            marked = _fail_if_stuck(session, task_id, durable_message)
        if marked and stream_message is not None and expected_stream_id is not None:
            task_stream.fail_if_active(
                task_id,
                message=stream_message,
                expected_stream_id=expected_stream_id,
            )


def job_generate(task_id: int, auto_review: bool = False) -> None:
    claim = _claim_job(
        task_id,
        expected_statuses=frozenset({"retrieving", "generating"}),
        require_stream=True,
    )
    if not claim.claimed:
        return
    try:
        with Session(get_engine()) as session:
            run_generate(session, task_id, chat_fn=None)
        if auto_review and _claim_auto_review(task_id, claim.launch_created_at):
            with Session(get_engine()) as review_session:
                run_review(review_session, task_id, chat_fn=None)
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


def job_review(task_id: int) -> None:
    claim = _claim_job(
        task_id,
        expected_statuses=frozenset({"reviewing"}),
        require_stream=False,
    )
    if not claim.claimed:
        return
    try:
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
    claim = _claim_job(
        task_id,
        expected_statuses=frozenset({"regenerating"}),
        require_stream=True,
    )
    if not claim.claimed:
        return
    try:
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
    claim = _claim_job(
        task_id,
        expected_statuses=frozenset({"optimizing"}),
        require_stream=False,
    )
    if not claim.claimed:
        return
    try:
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
