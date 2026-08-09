"""Persistent, single-worker runners for Wiki ingest jobs.

The database row is the queue.  The in-process executor only wakes a worker
for a job id; a worker always opens a fresh SQLModel Session and re-reads that
row before doing work.  This keeps browser requests independent from the
long-running LLM call and makes queued/running jobs recoverable after restart.
"""

from __future__ import annotations

import logging
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlmodel import Session, select

from app.db import get_engine
from app import config
from app.models.entities import Document, IngestJob, PromptTemplate
from app.services.wiki_ingest import IngestCancelled, _append_step, ingest_document

logger = logging.getLogger(__name__)

Runner = Callable[[int], Any]


def build_ingest_fingerprint(session: Session, document: Document) -> dict[str, Any]:
    """Describe inputs whose equality makes a completed ingest reusable."""

    digest = hashlib.sha256()
    for filename in ("purpose.md", "schema.md"):
        path = config.WIKI_DIR / filename
        digest.update(filename.encode("utf-8"))
        digest.update(path.read_bytes() if path.exists() else b"")
    prompts = session.exec(
        select(PromptTemplate)
        .where(
            PromptTemplate.type.in_(["wiki_analyze", "wiki_write"]),
            PromptTemplate.is_active == True,  # noqa: E712
        )
        .order_by(PromptTemplate.type, PromptTemplate.id)
    ).all()
    return {
        "source_sha256": document.sha256,
        "space_id": int(document.space_id) if document.space_id is not None else None,
        "wiki_config_sha256": digest.hexdigest(),
        "prompts": [f"{p.type}:{p.id}:v{p.version}" for p in prompts],
    }


def job_matches_fingerprint(job: IngestJob, fingerprint: dict[str, Any]) -> bool:
    try:
        payload = json.loads(job.plan_json or "{}")
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("ingest_fingerprint") == fingerprint


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class WikiJobScheduler:
    """One daemon worker per process; jobs execute serially."""

    def __init__(self, runner: Optional[Runner] = None) -> None:
        self.runner = runner
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="casegen-wiki-ingest",
        )
        self._lock = threading.RLock()
        self._pending: set[int] = set()
        self._active: set[int] = set()

    def schedule(self, job_id: int) -> bool:
        """Wake the worker once; duplicate wakeups are harmless and ignored."""
        with self._lock:
            if job_id in self._pending or job_id in self._active:
                return False
            self._pending.add(job_id)
            try:
                self._executor.submit(self._run, job_id)
            except Exception:
                self._pending.discard(job_id)
                raise
            return True

    def is_active(self, job_id: int) -> bool:
        with self._lock:
            return job_id in self._active

    def is_pending_or_active(self, job_id: int) -> bool:
        with self._lock:
            return job_id in self._pending or job_id in self._active

    def _run(self, job_id: int) -> None:
        with self._lock:
            self._pending.discard(job_id)
            self._active.add(job_id)
        try:
            (self.runner or _runner_override or run_ingest_job)(job_id)
        except Exception:
            # The normal runner persists failures itself.  This guard covers
            # injected runners and unexpected scheduler-level errors.
            logger.exception("wiki ingest worker failed job_id=%s", job_id)
            _mark_job_failed(job_id, "后台摄入 worker 异常")
        finally:
            with self._lock:
                self._active.discard(job_id)


_runtime_lock = threading.RLock()
_default_scheduler: Optional[WikiJobScheduler] = None
_scheduler_override: Any = None
_runner_override: Optional[Runner] = None


def set_scheduler(scheduler: Any) -> Any:
    """Inject a scheduler in tests; pass ``None`` to restore production use."""
    global _scheduler_override
    previous = _scheduler_override
    _scheduler_override = scheduler
    return previous


def set_runner(runner: Optional[Runner]) -> Optional[Runner]:
    """Inject a deterministic runner for tests without using a real thread."""
    global _runner_override
    previous = _runner_override
    _runner_override = runner
    return previous


def _get_default_scheduler() -> WikiJobScheduler:
    global _default_scheduler
    with _runtime_lock:
        if _default_scheduler is None:
            _default_scheduler = WikiJobScheduler()
        return _default_scheduler


def _scheduler_target() -> Any:
    return _scheduler_override if _scheduler_override is not None else _get_default_scheduler()


def schedule_ingest_job(job_id: int) -> Any:
    """Schedule a job using the injectable scheduler contract."""
    target = _scheduler_target()
    if hasattr(target, "schedule"):
        return target.schedule(job_id)
    if callable(target):
        return target(job_id)
    raise TypeError("scheduler must expose schedule(job_id) or be callable")


def _mark_job_failed(job_id: int, message: str) -> None:
    try:
        with Session(get_engine()) as session:
            job = session.get(IngestJob, job_id)
            if job is None or job.status in (
                "success",
                "success_with_warnings",
                "failed",
                "cancelled",
            ):
                return
            job.status = "failed"
            job.stage = "failed"
            job.error_message = message[:2000]
            job.updated_at = _utcnow()
            session.add(job)
            session.commit()
    except Exception:
        logger.exception("could not mark wiki job failed job_id=%s", job_id)


def run_ingest_job(job_id: int, *, chat_fn: Any = None) -> Optional[IngestJob]:
    """Execute one job with a new Session (the production worker entrypoint)."""
    with Session(get_engine()) as session:
        job = session.get(IngestJob, job_id)
        if job is None:
            return None
        if job.status in ("success", "success_with_warnings", "failed", "cancelled"):
            return job
        if job.cancel_requested:
            return cancel_ingest_job(session, job_id)
        if job.status not in ("queued", "running"):
            return job
        # Claim is intentionally persisted before parse/LLM work.  The
        # scheduler is single-process/single-worker; SQLite remains the
        # durable source of truth for recovery and API polling.
        job.status = "running"
        job.stage = "parsing"
        job.progress = max(1, int(job.progress or 0))
        job.updated_at = _utcnow()
        session.add(job)
        session.commit()
        session.refresh(job)
        return ingest_document(session, job.document_id, chat_fn=chat_fn, job=job)


def cancel_ingest_job(session: Session, job_id: int) -> Optional[IngestJob]:
    """Request cancellation or cancel a queued job immediately.

    A running LLM HTTP request is not forcibly interrupted.  The worker sees
    ``cancel_requested`` after that request returns and stops at the next
    stage/window boundary.
    """
    job = session.get(IngestJob, job_id)
    if job is None:
        return None
    if job.status in ("success", "success_with_warnings", "failed", "cancelled"):
        return job

    job.cancel_requested = True
    if job.status == "queued":
        job.status = "cancelled"
        job.stage = "cancelled"
        job.error_message = "Ingest cancelled"
        _append_step(job, "cancelled", "Queued ingest cancelled before execution")
        document = session.get(Document, job.document_id)
        if document is not None:
            document.status = "cancelled"
            document.error_message = None
            document.updated_at = _utcnow()
            session.add(document)
    job.updated_at = _utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def retry_failed_windows(session: Session, job_id: int) -> Optional[IngestJob]:
    """Queue a partial ingest again while reusing completed analyze windows."""

    job = session.get(IngestJob, job_id)
    if job is None:
        return None
    if job.status not in {"success_with_warnings", "failed"}:
        raise ValueError("只有已完成但有提示或失败的任务可以重试窗口")

    try:
        payload = json.loads(job.plan_json or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    results: list[dict[str, Any]] = []
    raw_results = payload.get("window_results")
    if isinstance(raw_results, list):
        results.extend(item for item in raw_results if isinstance(item, dict))
    if not results:
        try:
            step_log = json.loads(job.step_log_json or "[]")
        except (TypeError, json.JSONDecodeError):
            step_log = []
        if isinstance(step_log, list):
            for entry in step_log:
                if not isinstance(entry, dict):
                    continue
                window_result = entry.get("window_result")
                if isinstance(window_result, dict):
                    results.append(window_result)
    if not results:
        raise ValueError("任务没有可恢复的分析窗口，请使用“重试”重新摄入全文")

    retry_indices: list[int] = []
    for result in results:
        try:
            index = int(result.get("index") or 0)
        except (TypeError, ValueError):
            continue
        if index > 0 and str(result.get("status") or "").lower() in {"degraded", "failed"}:
            retry_indices.append(index)
    payload["window_results"] = results
    payload["degraded_windows"] = retry_indices
    payload["retry_failed_windows"] = True
    job.plan_json = json.dumps(payload, ensure_ascii=False)
    job.status = "queued"
    job.stage = "queued"
    job.progress = 0
    job.cancel_requested = False
    job.error_message = None
    _append_step(
        job,
        "retry_requested",
        f"已提交失败窗口重试（{len(retry_indices)} 个已标记窗口）",
        retry_windows=retry_indices,
    )
    document = session.get(Document, job.document_id)
    if document is not None:
        document.status = "ingesting"
        document.error_message = None
        document.updated_at = _utcnow()
        session.add(document)
    job.updated_at = _utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)
    try:
        schedule_ingest_job(job.id)
    except Exception:
        logger.exception("could not schedule wiki retry job_id=%s", job.id)
        _mark_job_failed(job.id, "无法调度失败窗口重试")
        session.refresh(job)
    return job


def recover_ingest_jobs() -> list[int]:
    """Recover durable queued/running rows during application initialization."""
    with Session(get_engine()) as session:
        jobs = list(
            session.exec(
                select(IngestJob)
                .where(IngestJob.status.in_(["queued", "running"]))
                .order_by(IngestJob.id)
            ).all()
        )

        target = _scheduler_target() if jobs else None
        recovered: list[int] = []
        for job in jobs:
            active = bool(
                target is not None
                and hasattr(target, "is_pending_or_active")
                and target.is_pending_or_active(job.id)
            )
            if job.status == "running" and not active:
                job.status = "queued"
                job.stage = "queued"
                job.progress = 0
                _append_step(job, "recovered", "Recovered after process restart")
                session.add(job)
                recovered.append(job.id)
            elif job.status == "queued":
                recovered.append(job.id)
        if recovered:
            session.commit()

    for job_id in recovered:
        try:
            schedule_ingest_job(job_id)
        except Exception:
            logger.exception("could not reschedule wiki job_id=%s", job_id)
            _mark_job_failed(job_id, "无法调度摄入任务")
    return recovered


def reset_scheduler_for_tests() -> None:
    """Clear injection state; intended for test teardown only."""
    global _scheduler_override, _runner_override
    _scheduler_override = None
    _runner_override = None
