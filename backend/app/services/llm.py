from __future__ import annotations

import time
from typing import Any

import httpx

from app.config import LLM_DEFAULT_TIMEOUT_SEC

# Transient gateway / upstream failures worth retrying
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_SEC = 1.5


class LLMError(Exception):
    pass


def build_chat_completions_url(base_url: str) -> str:
    """Normalize OpenAI-compatible base_url to a chat completions endpoint.

    Accepts any of:
    - https://host
    - https://host/v1
    - https://host/v1/chat/completions
    """
    root = (base_url or "").strip().rstrip("/")
    if not root:
        raise LLMError("base_url is empty")
    if root.endswith("/chat/completions"):
        return root
    # Already an OpenAI-style version root
    if root.endswith("/v1") or root.endswith("/v1beta"):
        return f"{root}/chat/completions"
    # Common gateway root without /v1 (e.g. CLI Proxy API Server)
    return f"{root}/v1/chat/completions"


def chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.2,
    timeout: float | None = None,
    transport: httpx.BaseTransport | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    backoff_sec: float = _DEFAULT_BACKOFF_SEC,
) -> tuple[str, dict[str, Any]]:
    url = build_chat_completions_url(base_url)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    last_error: Exception | None = None
    attempts = max(1, int(max_retries) + 1)

    for attempt in range(attempts):
        try:
            with httpx.Client(
                timeout=timeout or LLM_DEFAULT_TIMEOUT_SEC, transport=transport
            ) as client:
                resp = client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as e:
            last_error = LLMError(f"LLM request timed out ({url}): {e}")
            if attempt + 1 < attempts:
                time.sleep(backoff_sec * (attempt + 1))
                continue
            raise last_error from e
        except httpx.HTTPError as e:
            last_error = LLMError(f"LLM request failed ({url}): {e}")
            if attempt + 1 < attempts:
                time.sleep(backoff_sec * (attempt + 1))
                continue
            raise last_error from e

        if resp.status_code in _RETRYABLE_STATUS:
            body = (resp.text or "")[:500]
            last_error = LLMError(f"LLM HTTP {resp.status_code} ({url}): {body}")
            if attempt + 1 < attempts:
                time.sleep(backoff_sec * (attempt + 1))
                continue
            raise last_error

        if resp.status_code >= 400:
            raise LLMError(
                f"LLM HTTP {resp.status_code} ({url}): {(resp.text or '')[:500]}"
            )

        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Unexpected LLM response shape: {data!r}") from e
        if not content:
            raise LLMError("Empty LLM content")
        usage = data.get("usage") or {}
        return content, usage

    raise last_error or LLMError(f"LLM request failed ({url})")
