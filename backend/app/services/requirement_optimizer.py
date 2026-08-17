"""AI-assisted requirement refinement, isolated from the existing prompt optimizer."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from sqlmodel import Session, col, select

from app.models.entities import ModelConfig, PromptTemplate
from app.services.llm import LLMError, chat_completion


def _resolve_model(session: Session, model_id: int | None) -> ModelConfig:
    if model_id is not None:
        row = session.get(ModelConfig, model_id)
        if row is None:
            raise ValueError("Model not found")
        return row
    row = session.exec(
        select(ModelConfig)
        .where(ModelConfig.is_default == True)  # noqa: E712
        .order_by(col(ModelConfig.id).desc())
    ).first()
    if row is None:
        row = session.exec(select(ModelConfig).order_by(col(ModelConfig.id).desc())).first()
    if row is None:
        raise ValueError("No ModelConfig available")
    return row


def _extract_json(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("模型未返回可解析的需求优化 JSON")


def optimize_requirement(
    session: Session,
    *,
    title: str,
    description: str,
    focus_tags: list[str] | None = None,
    model_id: int | None = None,
    chat_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    prompt = session.exec(
        select(PromptTemplate)
        .where(
            PromptTemplate.type == "requirement_optimize",
            PromptTemplate.is_active == True,  # noqa: E712
        )
        .order_by(col(PromptTemplate.updated_at).desc(), col(PromptTemplate.id).desc())
    ).first()
    if prompt is None:
        raise ValueError("No active requirement_optimize prompt template found")
    model = _resolve_model(session, model_id)
    tags = ", ".join(str(item).strip() for item in (focus_tags or []) if str(item).strip())
    user = (
        "[USER_REQUIREMENT_START]\n"
        f"标题：{title.strip()}\n"
        f"描述：{description.strip()}\n"
        f"关注标签：{tags or '无'}\n"
        "[USER_REQUIREMENT_END]\n"
        "请严格按系统 JSON 契约返回。"
    )
    messages = [
        {"role": "system", "content": prompt.content},
        {"role": "user", "content": user},
    ]
    if chat_fn is not None:
        try:
            raw = chat_fn(messages=messages, model=model)
        except TypeError:
            raw = chat_fn(messages)
        if isinstance(raw, tuple):
            raw = raw[0]
    else:
        raw, _usage = chat_completion(
            base_url=model.base_url,
            api_key=model.api_key,
            model=model.model_name,
            messages=messages,
            temperature=0.2,
        )
    payload = _extract_json(str(raw))
    optimized_title = str(payload.get("title") or title).strip()
    optimized_description = str(payload.get("description") or description).strip()
    questions = payload.get("questions") or payload.get("pending_questions") or []
    if isinstance(questions, str):
        questions = [questions]
    if not isinstance(questions, list):
        questions = []
    if not optimized_title or not optimized_description:
        raise LLMError("需求优化结果缺少标题或描述")
    return {
        "title": optimized_title[:120],
        "description": optimized_description[:20000],
        "questions": [str(item).strip()[:500] for item in questions if str(item).strip()][:30],
        "prompt_type": "requirement_optimize",
    }
