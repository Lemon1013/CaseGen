from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any, Callable

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


def build_responses_url(base_url: str) -> str:
    """Normalize base_url to OpenAI-compatible Responses API endpoint (/v1/responses).

    Accepts any of:
    - https://host
    - https://host/v1
    - https://host/v1/responses
    - https://host/responses
    """
    root = (base_url or "").strip().rstrip("/")
    if not root:
        raise LLMError("base_url is empty")
    if root.endswith("/responses"):
        return root
    if root.endswith("/v1") or root.endswith("/v1beta"):
        return f"{root}/responses"
    return f"{root}/v1/responses"


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
    msg = f"LLM HTTP {resp.status_code} ({url}): {body}"
    if "/v1/chat/completions endpoint not supported" in body or (
        "chat/completions" in url and "endpoint not supported" in body.lower()
    ):
        msg += "（提示：上游渠道不支持 /v1/chat/completions，请在模型配置中将接口协议切换为「Responses API (/v1/responses)」）"
    elif "/v1/responses endpoint not supported" in body or (
        "responses" in url and "endpoint not supported" in body.lower()
    ):
        msg += "（提示：上游渠道不支持 /v1/responses，请在模型配置中将接口协议切换为「Chat Completions (/v1/chat/completions)」）"
    return error_type(msg)


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


def _build_responses_payload(
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.2,
    stream: bool = False,
    max_tokens: int | None = None,
    thinking: bool | None = None,
    response_format: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct a payload conforming to the OpenAI Responses API (/v1/responses)."""
    system_parts: list[str] = []
    input_messages: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system":
            system_parts.append(msg.get("content") or "")
        else:
            input_messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})

    payload: dict[str, Any] = {
        "model": model,
        "input": input_messages if input_messages else messages,
        "temperature": temperature,
    }
    if system_parts:
        payload["instructions"] = "\n\n".join(system_parts)
    if stream:
        payload["stream"] = True
    if max_tokens is not None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        payload["max_output_tokens"] = int(max_tokens)
    if thinking is not None:
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if response_format is not None:
        payload["response_format"] = dict(response_format)
    return payload


def _parse_responses_payload(data: Any) -> tuple[str, dict[str, Any]]:
    """Parse output from OpenAI Responses API non-streaming JSON response."""
    if not isinstance(data, dict):
        raise LLMError(f"Unexpected Responses API payload shape: {data!r}")
    # 1. Top-level output_text shorthand
    if data.get("output_text"):
        usage = data.get("usage") or {}
        return str(data["output_text"]), usage if isinstance(usage, dict) else {}
    # 2. Output items array
    output = data.get("output")
    if isinstance(output, list) and output:
        parts: list[str] = []
        for item in output:
            if isinstance(item, Mapping):
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, Mapping):
                            text = part.get("text") or part.get("output_text")
                            if isinstance(text, str):
                                parts.append(text)
                        elif isinstance(part, str):
                            parts.append(part)
                elif isinstance(content, str):
                    parts.append(content)
        content_str = "".join(parts)
        if content_str:
            usage = data.get("usage") or {}
            return content_str, usage if isinstance(usage, dict) else {}
    # 3. Fallback to choices if gateway returned chat-like envelope
    if "choices" in data:
        return _parse_chat_response(data)
    raise LLMError(f"Empty or unrecognized Responses API output: {data!r}")


def _parse_stream_response(
    resp: httpx.Response,
    *,
    on_delta: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    usage: dict[str, Any] = {}
    reasoning_chars = 0
    finish_reason: str | None = None
    saw_done = False
    for line in resp.iter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            saw_done = True
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

        # Handle Responses API SSE events (e.g. response.output_text.delta, response.completed)
        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            text = ""
            if isinstance(delta, str):
                text = delta
            elif isinstance(delta, Mapping):
                text = str(delta.get("text") or delta.get("content") or "")
            if text:
                parts.append(text)
                if on_delta is not None:
                    on_delta(text)
            continue
        if event_type in ("response.completed", "response.done"):
            saw_done = True
            resp_obj = event.get("response")
            if isinstance(resp_obj, Mapping):
                resp_usage = resp_obj.get("usage")
                if isinstance(resp_usage, dict):
                    usage = resp_usage
            continue

        choices = event.get("choices") or []
        if not choices or not isinstance(choices[0], Mapping):
            continue
        choice = choices[0]
        message = choice.get("delta") or choice.get("message") or {}
        if isinstance(message, Mapping):
            text = _content_text(message.get("content"))
            if text:
                parts.append(text)
                if on_delta is not None:
                    on_delta(text)
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
    if not saw_done and finish_reason is None:
        # A clean TCP EOF is not an application-level completion signal. Some
        # gateways truncate an SSE response without surfacing a protocol error;
        # treating those partial tokens as complete would persist a bad draft.
        raise _RetryableLLMError(
            "LLM stream ended before [DONE] or a finish_reason"
        )
    return content, usage


def chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    protocol: str = "chat_completions",
    temperature: float = 0.2,
    timeout: float | None = None,
    transport: httpx.BaseTransport | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    backoff_sec: float = _DEFAULT_BACKOFF_SEC,
    stream: bool = False,
    max_tokens: int | None = None,
    thinking: bool | None = None,
    response_format: Mapping[str, Any] | None = None,
    on_attempt: Callable[[int, bool], None] | None = None,
    on_delta: Callable[[str], None] | None = None,
    on_retry: Callable[[int, str], None] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Call an OpenAI-compatible chat or responses endpoint.

    ``protocol`` can be 'chat_completions' (default) or 'responses'.
    ``on_attempt(attempt, reset)`` is invoked before each upstream attempt.
    ``reset`` is true from the second attempt onward, so live consumers can
    discard tokens that arrived before a disconnected/retryable attempt.
    ``on_delta`` receives completed content fragments (including a single
    fragment when a provider ignores ``stream=true`` and returns JSON), and
    ``on_retry(next_attempt, message)`` is emitted before retry backoff.
    Callbacks are optional; existing callers retain the same return contract.
    """
    is_responses = (
        protocol == "responses"
        or (base_url and base_url.strip().rstrip("/").endswith("/responses"))
    )

    if is_responses:
        # Codex / Responses API reverse proxies and channels require stream=true.
        stream = True
        url = build_responses_url(base_url)
        payload = _build_responses_payload(
            model=model,
            messages=messages,
            temperature=temperature,
            stream=True,
            max_tokens=max_tokens,
            thinking=thinking,
            response_format=response_format,
        )
    else:
        url = build_chat_completions_url(base_url)
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

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    last_error: Exception | None = None
    attempts = max(1, int(max_retries) + 1)

    parse_fn = _parse_responses_payload if is_responses else _parse_chat_response

    with httpx.Client(
        timeout=timeout or LLM_DEFAULT_TIMEOUT_SEC,
        transport=transport,
    ) as client:
        for attempt in range(attempts):
            try:
                if on_attempt is not None:
                    on_attempt(attempt + 1, attempt > 0)
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
                            return _parse_stream_response(resp, on_delta=on_delta)
                        data = json.loads(resp.read())
                        content, usage = parse_fn(data)
                        if on_delta is not None:
                            on_delta(content)
                        return content, usage

                resp = client.post(url, headers=headers, json=payload)
                error = _response_error(resp, url)
                if error is not None:
                    raise error
                return parse_fn(resp.json())
            except _RetryableLLMError as exc:
                last_error = exc
            except httpx.TimeoutException as exc:
                last_error = LLMError(f"LLM request timed out ({url}): {exc}")
            except httpx.HTTPError as exc:
                last_error = LLMError(f"LLM request failed ({url}): {exc}")

            if attempt + 1 < attempts:
                if on_retry is not None:
                    on_retry(attempt + 2, str(last_error or "LLM request failed"))
                time.sleep(backoff_sec * (attempt + 1))
                continue
            if last_error is not None:
                raise last_error

    raise last_error or LLMError(f"LLM request failed ({url})")
