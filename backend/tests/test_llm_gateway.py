import json

import httpx
import pytest

from app.services.llm import LLMError, build_chat_completions_url, chat_completion


def test_build_chat_url_adds_v1_when_missing():
    assert (
        build_chat_completions_url("http://gpt.example.com")
        == "http://gpt.example.com/v1/chat/completions"
    )
    assert (
        build_chat_completions_url("http://gpt.example.com/v1")
        == "http://gpt.example.com/v1/chat/completions"
    )
    assert (
        build_chat_completions_url("http://gpt.example.com/v1/chat/completions")
        == "http://gpt.example.com/v1/chat/completions"
    )
    # Production gateway style used by CLI Proxy API Server
    assert (
        build_chat_completions_url("http://gpt.158918.xyz")
        == "http://gpt.158918.xyz/v1/chat/completions"
    )
    assert (
        build_chat_completions_url("http://gpt.158918.xyz/")
        == "http://gpt.158918.xyz/v1/chat/completions"
    )


def test_build_chat_url_rejects_empty():
    with pytest.raises(LLMError, match="empty"):
        build_chat_completions_url("  ")


def test_chat_completion_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://api.example.com/v1/chat/completions"
        assert request.headers.get("Authorization") == "Bearer sk-test"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    content, usage = chat_completion(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="gpt-test",
        messages=[{"role": "user", "content": "hi"}],
        transport=transport,
    )
    assert content == "hello"
    assert usage["prompt_tokens"] == 1


def test_chat_completion_streams_and_caps_output():
    deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["max_tokens"] == 64
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
                b'data: {"choices":[],"usage":{"completion_tokens":2}}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    content, usage = chat_completion(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="gpt-test",
        messages=[{"role": "user", "content": "hi"}],
        transport=httpx.MockTransport(handler),
        stream=True,
        max_tokens=64,
        thinking=False,
        response_format={"type": "json_object"},
        on_delta=deltas.append,
    )

    assert content == "hello"
    assert usage["completion_tokens"] == 2
    assert deltas == ["hel", "lo"]


def test_stream_request_falls_back_to_plain_json_and_emits_completed_delta():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "choices": [{"message": {"content": "plain response"}}],
                "usage": {"completion_tokens": 3},
            },
        )

    attempts: list[tuple[int, bool]] = []
    deltas: list[str] = []
    content, usage = chat_completion(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="gpt-test",
        messages=[{"role": "user", "content": "hi"}],
        transport=httpx.MockTransport(handler),
        stream=True,
        on_attempt=lambda attempt, reset: attempts.append((attempt, reset)),
        on_delta=deltas.append,
    )

    assert content == "plain response"
    assert usage == {"completion_tokens": 3}
    assert attempts == [(1, False)]
    assert deltas == ["plain response"]


def test_chat_completion_retries_stream_disconnect_without_partial_output():
    calls = {"n": 0}
    callbacks: list[tuple] = []
    preview: list[str] = []

    class DisconnectingStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.RemoteProtocolError("peer disconnected")

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=DisconnectingStream(),
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"choices":[{"delta":{"content":"recovered"}}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    def on_attempt(attempt: int, reset: bool) -> None:
        callbacks.append(("attempt", attempt, reset))
        if reset:
            preview.clear()

    def on_delta(delta: str) -> None:
        callbacks.append(("delta", delta))
        preview.append(delta)

    def on_retry(attempt: int, message: str) -> None:
        callbacks.append(("retry", attempt, message))

    content, _ = chat_completion(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="gpt-test",
        messages=[{"role": "user", "content": "hi"}],
        transport=httpx.MockTransport(handler),
        stream=True,
        max_retries=1,
        backoff_sec=0,
        on_attempt=on_attempt,
        on_delta=on_delta,
        on_retry=on_retry,
    )

    assert content == "recovered"
    assert "".join(preview) == "recovered"
    assert calls["n"] == 2
    assert callbacks[0:2] == [("attempt", 1, False), ("delta", "partial")]
    assert callbacks[2][0:2] == ("retry", 2)
    assert callbacks[3:] == [
        ("attempt", 2, True),
        ("delta", "recovered"),
    ]


def test_chat_completion_retries_clean_eof_without_completion_signal():
    calls = {"n": 0}
    attempts: list[tuple[int, bool]] = []
    retries: list[int] = []
    preview: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"choices":[{"delta":{"content":"complete"},'
                b'"finish_reason":"stop"}]}\n\n'
            ),
        )

    def on_attempt(attempt: int, reset: bool) -> None:
        attempts.append((attempt, reset))
        if reset:
            preview.clear()

    content, _ = chat_completion(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="gpt-test",
        messages=[{"role": "user", "content": "hi"}],
        transport=httpx.MockTransport(handler),
        stream=True,
        max_retries=1,
        backoff_sec=0,
        on_attempt=on_attempt,
        on_delta=preview.append,
        on_retry=lambda attempt, message: retries.append(attempt),
    )

    assert content == "complete"
    assert "".join(preview) == "complete"
    assert attempts == [(1, False), (2, True)]
    assert retries == [2]
    assert calls["n"] == 2


def test_chat_completion_accepts_finish_reason_without_done_marker():
    response = (
        b'data: {"choices":[{"delta":{"content":"complete"},'
        b'"finish_reason":"stop"}]}\n\n'
    )
    content, _ = chat_completion(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="gpt-test",
        messages=[{"role": "user", "content": "hi"}],
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=response,
            )
        ),
        stream=True,
        max_retries=0,
    )
    assert content == "complete"


def test_chat_completion_reports_reasoning_budget_exhaustion():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n'
                b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    with pytest.raises(LLMError, match="reached max_tokens"):
        chat_completion(
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "hi"}],
            transport=httpx.MockTransport(handler),
            stream=True,
            max_retries=0,
        )


def test_chat_completion_root_base_url_uses_v1_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://gpt.example.com/v1/chat/completions"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
        )

    transport = httpx.MockTransport(handler)
    content, _ = chat_completion(
        base_url="http://gpt.example.com",
        api_key="sk-test",
        model="grok-4.5",
        messages=[{"role": "user", "content": "hi"}],
        transport=transport,
    )
    assert content == "ok"


def test_chat_completion_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    transport = httpx.MockTransport(handler)
    with pytest.raises(LLMError, match="LLM HTTP 400"):
        chat_completion(
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
            transport=transport,
            max_retries=0,
        )


def test_chat_completion_retries_502_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(502, text="")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "recovered"}}], "usage": {}},
        )

    transport = httpx.MockTransport(handler)
    content, _ = chat_completion(
        base_url="https://api.example.com/v1",
        api_key="sk-test",
        model="gpt-test",
        messages=[{"role": "user", "content": "hi"}],
        transport=transport,
        max_retries=3,
        backoff_sec=0,
    )
    assert content == "recovered"
    assert calls["n"] == 3


def test_chat_completion_502_exhausted_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="")

    transport = httpx.MockTransport(handler)
    with pytest.raises(LLMError, match="LLM HTTP 502"):
        chat_completion(
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
            transport=transport,
            max_retries=2,
            backoff_sec=0,
        )
