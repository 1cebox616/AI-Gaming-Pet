"""OpenRouter client tests use only httpx's in-memory transport."""

import json

import httpx
import pytest

from pet.llm import LlmError, OpenRouterClient


def test_complete_parses_text_usage_cost_and_actual_routing() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["stream"] is False
        assert payload["model"] == "vendor/model-under-test"
        assert "provider" not in payload
        return httpx.Response(
            200,
            json={
                "model": "vendor/model-actual",
                "provider": "provider-under-test",
                "choices": [{"message": {"content": "好枪兄弟好枪"}}],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 8,
                    "cost": 0.00125,
                },
            },
        )

    client = OpenRouterClient(
        "test-api-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.complete(
            model="vendor/model-under-test",
            system_prompt="系统提示",
            user_prompt="用户提示",
            max_tokens=32,
            temperature=0.9,
        )
    finally:
        client.close()

    assert len(requests) == 1
    assert result.text == "好枪兄弟好枪"
    assert result.model == "vendor/model-actual"
    assert result.provider == "provider-under-test"
    assert result.usage.prompt_tokens == 120
    assert result.usage.completion_tokens == 8
    assert result.usage.cost_usd == pytest.approx(0.00125)
    assert result.latency_seconds >= 0


def test_provider_lock_uses_only_requested_provider_and_disables_fallbacks() -> None:
    request_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "vendor/model-actual",
                "provider": "provider-under-test",
                "choices": [{"message": {"content": "锁定成功"}}],
            },
        )

    client = OpenRouterClient("test-api-key", transport=httpx.MockTransport(handler))
    try:
        client.complete(
            model="vendor/model-under-test",
            provider="provider-under-test",
            system_prompt="system",
            user_prompt="user",
            max_tokens=32,
            temperature=0.9,
        )
    finally:
        client.close()

    assert request_body["provider"] == {
        "only": ["provider-under-test"],
        "allow_fallbacks": False,
    }


def test_unavailable_locked_provider_fails_once_without_fallback() -> None:
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            503,
            json={"error": {"message": "No endpoints found matching your data policy"}},
        )

    client = OpenRouterClient("test-api-key", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(LlmError, match="No endpoints found") as caught:
            client.complete(
                model="vendor/model-under-test",
                provider="unavailable-provider",
                system_prompt="system",
                user_prompt="user",
                max_tokens=32,
                temperature=0.9,
            )
    finally:
        client.close()

    assert request_count == 1
    assert caught.value.status_code == 503


def test_upstream_error_raises_once_without_retry() -> None:
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    client = OpenRouterClient(
        "test-api-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(LlmError, match="rate limited") as caught:
            client.complete(
                model="vendor/model-under-test",
                system_prompt="system",
                user_prompt="user",
                max_tokens=32,
                temperature=0.9,
            )
    finally:
        client.close()

    assert request_count == 1
    assert caught.value.status_code == 429
    assert caught.value.latency_seconds is not None


def test_missing_upstream_accounting_stays_unknown() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "vendor/model-actual",
                "choices": [{"message": {"content": "短句"}}],
            },
        )

    client = OpenRouterClient(
        "test-api-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.complete(
            model="vendor/model-under-test",
            system_prompt="system",
            user_prompt="user",
            max_tokens=32,
            temperature=0.9,
        )
    finally:
        client.close()

    assert result.usage.prompt_tokens is None
    assert result.usage.completion_tokens is None
    assert result.usage.cost_usd is None
    assert result.provider is None


def test_timeout_raises_once_without_retry() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client = OpenRouterClient(
        "test-api-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(LlmError, match="请求超时") as caught:
            client.complete(
                model="vendor/model-under-test",
                system_prompt="system",
                user_prompt="user",
                max_tokens=32,
                temperature=0.9,
            )
    finally:
        client.close()

    assert request_count == 1
    assert caught.value.status_code is None
    assert caught.value.latency_seconds is not None


def test_missing_environment_variable_has_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(LlmError, match="未设置环境变量 OPENROUTER_API_KEY"):
        OpenRouterClient.from_env()
