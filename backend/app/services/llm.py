from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

import httpx

from app.config import LLM_DEFAULT_TIMEOUT_SEC

# Transient gateway / upstream failures worth retrying
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_SEC = 1.5


class LLMError(Exception):
    pass


class _RetryableLLMError(LLMError):
    """Internal marker for HTTP statuses that are safe to retry."""


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


def _response_error(resp: httpx.Response, url: str) -> LLMError | None:
    if resp.status_code < 400:
        return None
    try:
        body = resp.read().decode("utf-8", errors="replace")[:500]
    except httpx.HTTPError:
        body = ""
    error_type = (
        _RetryableLLMError if resp.status_code in _RETRYABLE_STATUS else LLMError
    )
    return error_type(f"LLM HTTP {resp.status_code} ({url}): {body}")


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, Mapping):
            text = item.get("text") or item.get("content")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _parse_chat_response(data: Any) -> tuple[str, dict[str, Any]]:
    try:
        message = data["choices"][0]["message"]
        content = _content_text(message.get("content"))
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Unexpected LLM response shape: {data!r}") from exc
    if not content:
        raise LLMError("Empty LLM content")
    usage = data.get("usage") or {}
    return content, usage if isinstance(usage, dict) else {}


def _parse_stream_response(resp: httpx.Response) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    usage: dict[str, Any] = {}
    reasoning_chars = 0
    finish_reason: str | None = None
    for line in resp.iter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            break
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError("Invalid JSON event in LLM stream") from exc
        if not isinstance(event, dict):
            continue
        if event.get("error"):
            raise LLMError(f"LLM stream error: {str(event['error'])[:500]}")
        event_usage = event.get("usage")
        if isinstance(event_usage, dict):
            usage = event_usage
        choices = event.get("choices") or []
        if not choices or not isinstance(choices[0], Mapping):
            continue
        choice = choices[0]
        message = choice.get("delta") or choice.get("message") or {}
        if isinstance(message, Mapping):
            text = _content_text(message.get("content"))
            if text:
                parts.append(text)
            reasoning = _content_text(
                message.get("reasoning_content") or message.get("reasoning")
            )
            reasoning_chars += len(reasoning)
        if choice.get("finish_reason"):
            finish_reason = str(choice["finish_reason"])
    content = "".join(parts)
    if finish_reason == "length":
        raise LLMError(
            "LLM stream reached max_tokens before completing the final content"
        )
    if not content:
        if reasoning_chars:
            raise LLMError(
                "LLM stream ended without final content after "
                f"{reasoning_chars} reasoning characters "
                f"(finish_reason={finish_reason or 'unknown'})"
            )
        raise LLMError("Empty LLM stream content")
    return content, usage


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
    stream: bool = False,
    max_tokens: int | None = None,
    thinking: bool | None = None,
    response_format: Mapping[str, Any] | None = None,
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
    if stream:
        payload["stream"] = True
    if max_tokens is not None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        payload["max_tokens"] = int(max_tokens)
    if thinking is not None:
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if response_format is not None:
        payload["response_format"] = dict(response_format)
    last_error: Exception | None = None
    attempts = max(1, int(max_retries) + 1)

    with httpx.Client(
        timeout=timeout or LLM_DEFAULT_TIMEOUT_SEC,
        transport=transport,
    ) as client:
        for attempt in range(attempts):
            try:
                if stream:
                    with client.stream(
                        "POST",
                        url,
                        headers=headers,
                        json=payload,
                    ) as resp:
                        error = _response_error(resp, url)
                        if error is not None:
                            raise error
                        content_type = resp.headers.get("content-type", "").lower()
                        if "text/event-stream" in content_type:
                            return _parse_stream_response(resp)
                        data = json.loads(resp.read())
                        return _parse_chat_response(data)

                resp = client.post(url, headers=headers, json=payload)
                error = _response_error(resp, url)
                if error is not None:
                    raise error
                return _parse_chat_response(resp.json())
            except _RetryableLLMError as exc:
                last_error = exc
            except httpx.TimeoutException as exc:
                last_error = LLMError(f"LLM request timed out ({url}): {exc}")
            except httpx.HTTPError as exc:
                last_error = LLMError(f"LLM request failed ({url}): {exc}")

            if attempt + 1 < attempts:
                time.sleep(backoff_sec * (attempt + 1))
                continue
            if last_error is not None:
                raise last_error

    raise last_error or LLMError(f"LLM request failed ({url})")
