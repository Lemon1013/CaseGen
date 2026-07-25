from __future__ import annotations

import json
from typing import Any, Optional

from sqlmodel import Session

from app.models.entities import TaskEvent


def append_event(
    session: Session,
    task_id: int,
    step: str,
    message: str,
    detail: Optional[Any] = None,
) -> TaskEvent:
    detail_json: Optional[str] = None
    if detail is not None:
        if isinstance(detail, str):
            detail_json = detail
        else:
            detail_json = json.dumps(detail, ensure_ascii=False, default=str)

    event = TaskEvent(
        task_id=task_id,
        step=step,
        message=message,
        detail_json=detail_json,
    )
    session.add(event)
    session.flush()
    return event
