from __future__ import annotations

from typing import Any

import httpx

from app.config import LLM_DEFAULT_TIMEOUT_SEC


class LLMError(Exception):
    pass


def chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.2,
    timeout: float | None = None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, dict[str, Any]]:
    root = base_url.rstrip("/")
    url = f"{root}/chat/completions" if not root.endswith("/chat/completions") else root
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    try:
        with httpx.Client(timeout=timeout or LLM_DEFAULT_TIMEOUT_SEC, transport=transport) as client:
            resp = client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        raise LLMError(f"LLM request failed: {e}") from e
    if resp.status_code >= 400:
        raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"Unexpected LLM response shape: {data!r}") from e
    if not content:
        raise LLMError("Empty LLM content")
    usage = data.get("usage") or {}
    return content, usage
