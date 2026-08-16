"""Background runners for long LLM pipeline steps.

Each job opens its own DB session so FastAPI request sessions can close
immediately after scheduling.
"""

from __future__ import annotations

import logging
import traceback
import threading
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlmodel import Session

from app.db import get_engine
from app.models.entities import (
    CaseDraft,
    GenerationTask,
    TaskRetrievalCheckpoint,
    TaskTestPointCheckpoint,
)
from sqlalchemy import exists, update, or_
from sqlmodel import select
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


def job_generate(
    task_id: int,
    auto_review: bool = False,
    checkpoint_id: int | None = None,
    claim_token: str | None = None,
    checkpoint_kind: str = "retrieval",
) -> None:
    claim = _claim_job(
        task_id,
        expected_statuses=frozenset({"retrieving", "generating_test_points", "generating"}),
        require_stream=True,
        checkpoint_id=checkpoint_id,
        claim_token=claim_token,
        checkpoint_kind=checkpoint_kind,
    )
    if not claim.claimed:
        return
    if not _consume_resume_claim(claim):
        return
    heartbeat_stop = threading.Event()
    heartbeat_thread = None
    if claim.checkpoint_id is not None and claim.claim_token is not None:
        heartbeat_thread = threading.Thread(
            target=_heartbeat_resume_claim,
            args=(claim.checkpoint_id, claim.claim_token, heartbeat_stop, 30.0, claim.checkpoint_kind),
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
        _finish_resume_claim(claim, "failed" if claim.checkpoint_id is not None else "completed")


def recover_generation_jobs() -> list[int]:
    """Resume either stage of the durable two-checkpoint generation flow."""

    with Session(get_engine()) as session:
        tasks = session.exec(
            select(GenerationTask)
            .where(GenerationTask.status.in_({"generating_test_points", "generating"}))
            .order_by(GenerationTask.id.asc())
        ).all()
        claimed_rows: list[tuple[int, int, bool, str, str]] = []
        now = _utcnow()
        for task in tasks:
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
            # Old databases/tasks may be mid-generation without a test-point
            # checkpoint.  Migrate them to the explicit test-point stage
            # before claiming the confirmed retrieval lease; never schedule a
            # legacy task directly into complete-case generation.
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
                and (current.resume_claimed_at is None or current.resume_claimed_at >= now - timedelta(minutes=5))
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
            claimed_rows.append((task_id, int(current.id), bool(getattr(current, "auto_review", False)), token, kind))
        session.commit()
    ids = [int(row[0]) for row in claimed_rows]
    for task_id, checkpoint_id, auto_review, token, kind in claimed_rows:
        status = "generating_test_points" if kind == "retrieval" else "generating"
        task_stream.start(task_id, status=status, message="恢复中断的生成任务")
        threading.Thread(
            target=job_generate,
            args=(task_id, auto_review, checkpoint_id, token, kind),
            daemon=True,
        ).start()
    return ids


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
