"""Profile endpoint and probe tests never access the network or reveal credentials."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pet.core.config import LlmConfig, LlmProfileConfig, resolve_llm_profile
from pet.core.llm import LlmError, OpenRouterClient, probe_llm_profile


FIXTURE = Path(__file__).parent / "fixtures" / "vision-exam-frame-a.ppm"


def _completion(*, stream: bool = False) -> httpx.Response:
    if stream:
        body = "".join(
            (
                'data: {"model":"same/model","choices":[{"delta":{"content":"OK"}}]}\n\n',
                'data: {"model":"same/model","choices":[{"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":10,"completion_tokens":1,'
                '"completion_tokens_details":{"reasoning_tokens":0}}}\n\n',
                "data: [DONE]\n\n",
            )
        )
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )
    return httpx.Response(
        200,
        json={
            "model": "same/model",
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        },
    )


def test_profile_resolves_custom_endpoint_and_credential_variable() -> None:
    configuration = LlmConfig(
        model="default/model",
        profiles={
            "nearby": LlmProfileConfig(
                model="same/model",
                base_url="https://endpoint.invalid/v1",
                api_key_env="NEARBY_MODEL_KEY",
            )
        },
    )
    profile = resolve_llm_profile(configuration, "nearby")
    assert profile.model == "same/model"
    assert profile.base_url == "https://endpoint.invalid/v1"
    assert profile.api_key_env == "NEARBY_MODEL_KEY"


def test_profile_client_uses_exact_environment_and_custom_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEARBY_MODEL_KEY", "credential-value-never-print")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _completion()

    client = OpenRouterClient.from_profile(
        profile_name="nearby",
        base_url="https://endpoint.invalid/v1",
        api_key_env="NEARBY_MODEL_KEY",
        timeout_seconds=3.0,
        transport=httpx.MockTransport(handler),
    )
    try:
        client.complete(
            model="same/model",
            provider="must-not-leak",
            system_prompt="system",
            user_prompt="user",
            max_tokens=8,
            temperature=0.0,
        )
    finally:
        client.close()
    assert requests[0].url.host == "endpoint.invalid"
    assert requests[0].headers["authorization"] == "Bearer credential-value-never-print"
    assert "provider" not in json.loads(requests[0].content)


def test_profile_client_missing_variable_names_profile_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEARBY_MODEL_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-be-used")
    with pytest.raises(
        LlmError,
        match="模型档位 nearby 缺少环境变量 NEARBY_MODEL_KEY",
    ):
        OpenRouterClient.from_profile(
            profile_name="nearby",
            base_url="https://endpoint.invalid/v1",
            api_key_env="NEARBY_MODEL_KEY",
            timeout_seconds=3.0,
        )


def test_default_endpoint_still_sends_provider_routing() -> None:
    body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body.update(json.loads(request.content))
        return _completion()

    client = OpenRouterClient("fake-key", transport=httpx.MockTransport(handler))
    try:
        client.complete(
            model="same/model",
            provider="route-id",
            system_prompt="system",
            user_prompt="user",
            max_tokens=8,
            temperature=0.0,
        )
    finally:
        client.close()
    assert body["provider"] == {"only": ["route-id"], "allow_fallbacks": False}


def test_probe_missing_environment_stops_without_printing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEARBY_MODEL_KEY", raising=False)
    lines: list[str] = []
    configuration = LlmConfig(
        model="default/model",
        profiles={
            "nearby": LlmProfileConfig(
                model="same/model",
                base_url="https://endpoint.invalid/v1",
                api_key_env="NEARBY_MODEL_KEY",
            )
        },
    )
    result = probe_llm_profile(
        configuration,
        "nearby",
        image_path=FIXTURE,
        output=lines.append,
    )
    rendered = "\n".join(lines)
    assert not result.passed
    assert "NEARBY_MODEL_KEY：未设置" in rendered
    assert "未尝试连接，也未回退" in rendered
    assert "credential" not in rendered


def test_probe_measures_reasoning_image_and_stream_without_provider_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "credential-value-never-print"
    monkeypatch.setenv("NEARBY_MODEL_KEY", secret)
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "same/model"}]})
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("reasoning") == {"enabled": False}:
            return httpx.Response(400, json={"error": {"message": "unsupported field"}})
        return _completion(stream=body.get("stream") is True)

    client = OpenRouterClient.from_profile(
        profile_name="nearby",
        base_url="https://endpoint.invalid/v1",
        api_key_env="NEARBY_MODEL_KEY",
        timeout_seconds=3.0,
        transport=httpx.MockTransport(handler),
    )
    lines: list[str] = []
    configuration = LlmConfig(
        model="default/model",
        profiles={
            "nearby": LlmProfileConfig(
                model="same/model",
                provider="route-id",
                base_url="https://endpoint.invalid/v1",
                api_key_env="NEARBY_MODEL_KEY",
            )
        },
    )
    try:
        result = probe_llm_profile(
            configuration,
            "nearby",
            image_path=FIXTURE,
            output=lines.append,
            injected_client=client,
        )
    finally:
        client.close()
    rendered = "\n".join(lines)
    assert result.passed
    assert result.selected_reasoning_mode == "omitted"
    assert result.ttft_ms is not None
    assert all("provider" not in body for body in bodies)
    assert any(body.get("reasoning") == {"effort": "none"} for body in bodies)
    assert any(body.get("reasoning") == {"enabled": False} for body in bodies)
    assert "图像调用：通过" in rendered
    assert "流式调用：通过" in rendered
    assert secret not in rendered
