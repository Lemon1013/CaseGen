import httpx
import pytest

from app.services.llm import LLMError, chat_completion


def test_chat_completion_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url).endswith("/chat/completions")
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


def test_chat_completion_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    with pytest.raises(LLMError):
        chat_completion(
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
            transport=transport,
        )
