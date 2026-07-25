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
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    with pytest.raises(LLMError, match="LLM HTTP 500"):
        chat_completion(
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
            transport=transport,
        )
