"""Background runners for long LLM pipeline steps.

Each job opens its own DB session so FastAPI request sessions can close
immediately after scheduling.
"""

from __future__ import annotations

import logging
import traceback
from typing import Optional

from sqlmodel import Session

from app.db import get_engine
from app.models.entities import GenerationTask
from app.services.task_pipeline import (
    run_generate,
    run_optimize_prompt,
    run_regenerate,
    run_review,
)

logger = logging.getLogger(__name__)


def _fail_if_stuck(session: Session, task_id: int, message: str) -> None:
    task = session.get(GenerationTask, task_id)
    if task is None:
        return
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


def job_generate(task_id: int, auto_review: bool = False) -> None:
    try:
        with Session(get_engine()) as session:
            run_generate(session, task_id, chat_fn=None)
            if auto_review:
                task = session.get(GenerationTask, task_id)
                if task is not None and task.status == "generated":
                    run_review(session, task_id, chat_fn=None)
    except Exception as exc:
        logger.exception("background generate failed task_id=%s", task_id)
        try:
            with Session(get_engine()) as session:
                _fail_if_stuck(
                    session,
                    task_id,
                    f"后台生成失败: {exc}\n{traceback.format_exc()[-500:]}",
                )
        except Exception:
            logger.exception("failed to mark task failed task_id=%s", task_id)


def job_review(task_id: int) -> None:
    try:
        with Session(get_engine()) as session:
            run_review(session, task_id, chat_fn=None)
    except Exception as exc:
        logger.exception("background review failed task_id=%s", task_id)
        try:
            with Session(get_engine()) as session:
                _fail_if_stuck(session, task_id, f"后台评审失败: {exc}")
        except Exception:
            logger.exception("failed to mark task failed task_id=%s", task_id)


def job_regenerate(task_id: int) -> None:
    try:
        with Session(get_engine()) as session:
            run_regenerate(session, task_id, chat_fn=None)
    except Exception as exc:
        logger.exception("background regenerate failed task_id=%s", task_id)
        try:
            with Session(get_engine()) as session:
                _fail_if_stuck(session, task_id, f"后台再生成失败: {exc}")
        except Exception:
            logger.exception("failed to mark task failed task_id=%s", task_id)


def job_optimize_prompt(task_id: int) -> None:
    try:
        with Session(get_engine()) as session:
            run_optimize_prompt(session, task_id, chat_fn=None)
    except Exception as exc:
        logger.exception("background optimize failed task_id=%s", task_id)
        try:
            with Session(get_engine()) as session:
                _fail_if_stuck(session, task_id, f"后台优化提示词失败: {exc}")
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
