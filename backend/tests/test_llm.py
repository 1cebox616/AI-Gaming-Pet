"""OpenRouter client tests use only httpx's in-memory transport."""

import json
import base64
from io import BytesIO

import httpx
from PIL import Image
import pytest

from pet.core.llm import LlmError, LlmImage, OpenRouterClient, parse_analysis_text


class _StepClock:
    def __init__(self, step: float = 0.01) -> None:
        self._value = -step
        self._step = step

    def __call__(self) -> float:
        self._value += self._step
        return self._value


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


def test_complete_forwards_optional_seed() -> None:
    request_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "vendor/model-actual",
                "choices": [{"message": {"content": "好"}}],
            },
        )

    client = OpenRouterClient("test-api-key", transport=httpx.MockTransport(handler))
    try:
        client.complete(
            model="vendor/model-under-test",
            system_prompt="system",
            user_prompt="user",
            max_tokens=32,
            temperature=0.9,
            seed=42,
        )
    finally:
        client.close()

    assert request_body["seed"] == 42


def test_complete_forwards_explicit_reasoning_effort() -> None:
    request_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "vendor/model-actual",
                "choices": [{"message": {"content": "好"}}],
            },
        )

    client = OpenRouterClient("test-api-key", transport=httpx.MockTransport(handler))
    try:
        client.complete(
            model="vendor/model-under-test",
            system_prompt="system",
            user_prompt="user",
            max_tokens=32,
            temperature=0.9,
            reasoning_effort="none",
        )
    finally:
        client.close()

    assert request_body["reasoning"] == {"effort": "none"}


def test_complete_can_explicitly_disable_reasoning() -> None:
    request_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "vendor/model-actual",
                "choices": [{"message": {"content": "好"}}],
            },
        )

    client = OpenRouterClient("test-api-key", transport=httpx.MockTransport(handler))
    try:
        client.complete(
            model="vendor/model-under-test",
            system_prompt="system",
            user_prompt="user",
            max_tokens=32,
            temperature=0.0,
            reasoning_enabled=False,
        )
    finally:
        client.close()

    assert request_body["reasoning"] == {"enabled": False}


def test_empty_visible_output_preserves_reasoning_usage_and_finish_reason() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "vendor/model-actual",
                "choices": [
                    {"message": {"content": None}, "finish_reason": "length"}
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 60,
                    "completion_tokens_details": {"reasoning_tokens": 60},
                    "cost": 0.001,
                },
            },
        )

    client = OpenRouterClient("test-api-key", transport=httpx.MockTransport(handler))
    try:
        result = client.complete(
            model="vendor/model-under-test",
            system_prompt="system",
            user_prompt="user",
            max_tokens=60,
            temperature=0.0,
        )
    finally:
        client.close()

    assert result.text == ""
    assert result.usage.completion_tokens == 60
    assert result.usage.reasoning_tokens == 60
    assert result.finish_reason == "length"


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


def test_complete_with_images_builds_content_blocks_and_resizes_locally(
    tmp_path,
) -> None:
    image_path = tmp_path / "wide.png"
    Image.new("RGB", (20, 10), (10, 20, 30)).save(image_path)
    request_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "vendor/vision-actual",
                "choices": [{"message": {"content": '{"scene":"x"}'}}],
            },
        )

    client = OpenRouterClient("test-api-key", transport=httpx.MockTransport(handler))
    try:
        client.complete_with_images(
            model="vendor/vision-under-test",
            system_prompt="system",
            user_prompt="observe",
            images=(LlmImage(image_path, "帧1"),),
            max_image_edge=8,
            max_tokens=64,
            temperature=0.0,
        )
    finally:
        client.close()

    messages = request_body["messages"]
    assert isinstance(messages, list)
    user_content = messages[1]["content"]
    assert [block["type"] for block in user_content] == [
        "text",
        "text",
        "image_url",
    ]
    assert user_content[1]["text"] == "帧1"
    data_url = user_content[2]["image_url"]["url"]
    assert data_url.startswith("data:image/png;base64,")
    encoded = data_url.partition(",")[2]
    with Image.open(BytesIO(base64.b64decode(encoded))) as uploaded:
        assert uploaded.size == (8, 4)


def test_complete_with_images_honors_smaller_per_attachment_limit(tmp_path) -> None:
    image_path = tmp_path / "wide.png"
    Image.new("RGB", (20, 10), (10, 20, 30)).save(image_path)
    uploaded_size: tuple[int, int] | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded_size
        payload = json.loads(request.content)
        data_url = payload["messages"][1]["content"][2]["image_url"]["url"]
        with Image.open(BytesIO(base64.b64decode(data_url.partition(",")[2]))) as uploaded:
            uploaded_size = uploaded.size
        return httpx.Response(
            200,
            json={
                "model": "vendor/vision-actual",
                "choices": [{"message": {"content": "ok"}}],
            },
        )

    client = OpenRouterClient("test-api-key", transport=httpx.MockTransport(handler))
    try:
        client.complete_with_images(
            model="vendor/vision-under-test",
            system_prompt="system",
            user_prompt="observe",
            images=(LlmImage(image_path, "帧1", max_edge=6),),
            max_image_edge=8,
            max_tokens=64,
            temperature=0.0,
        )
    finally:
        client.close()

    assert uploaded_size == (6, 3)


def test_streamed_analysis_parses_three_fields_usage_and_event_line_latency() -> None:
    request_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_body.update(json.loads(request.content))
        body = "".join(
            (
                ': OPENROUTER PROCESSING\n\n',
                'data: {"model":"vendor/model-actual","provider":"fast-provider",'
                '"choices":[{"delta":{"content":"核对：类型=爆头击杀；武器=AK47\\n"}}]}\n\n',
                'data: {"model":"vendor/model-actual","provider":"fast-provider",'
                '"choices":[{"delta":{"content":"事件：爆头击杀\\n"}}]}\n\n',
                'data: {"model":"vendor/model-actual","provider":"fast-provider",'
                '"choices":[{"delta":{"content":"场面：掉血后紧接着完成击杀。比分仍然落后。"}}]}\n\n',
                'data: {"model":"vendor/model-actual","provider":"fast-provider",'
                '"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":20,'
                '"cost":0.001}}\n\n',
                "data: [DONE]\n\n",
            )
        )
        return httpx.Response(200, text=body)

    client = OpenRouterClient(
        "test-api-key",
        transport=httpx.MockTransport(handler),
        clock=_StepClock(),
    )
    try:
        result = client.analyze_stream(
            model="vendor/model-under-test",
            provider="fast-provider",
            system_prompt="system",
            user_prompt="card",
            max_tokens=192,
            temperature=0.2,
            event_timeout_seconds=3.0,
            full_timeout_seconds=6.0,
            seed=42,
            reasoning_effort="low",
        )
    finally:
        client.close()

    assert request_body["stream"] is True
    assert request_body["reasoning"] == {"effort": "low"}
    assert request_body["seed"] == 42
    assert request_body["provider"] == {
        "only": ["fast-provider"],
        "allow_fallbacks": False,
    }
    assert result.audit_text == "类型=爆头击杀；武器=AK47"
    assert result.event_text == "爆头击杀"
    assert result.scene_text == "掉血后紧接着完成击杀。比分仍然落后。"
    assert 0 <= result.event_latency_seconds < result.latency_seconds
    assert result.model == "vendor/model-actual"
    assert result.provider == "fast-provider"
    assert result.usage.prompt_tokens == 100
    assert result.usage.completion_tokens == 20
    assert result.usage.cost_usd == pytest.approx(0.001)


def test_streamed_analysis_event_deadline_aborts_without_retry() -> None:
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            text=(
                'data: {"model":"vendor/model-actual",'
                '"choices":[{"delta":{"content":"核对：类型=击杀\\n"}}]}\n\n'
                'data: {"model":"vendor/model-actual",'
                '"choices":[{"delta":{"content":"事件：太慢了\\n"}}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    client = OpenRouterClient(
        "test-api-key",
        transport=httpx.MockTransport(handler),
        clock=_StepClock(step=0.1),
    )
    try:
        with pytest.raises(LlmError, match="事件行超时"):
            client.analyze_stream(
                model="vendor/model-under-test",
                system_prompt="system",
                user_prompt="card",
                max_tokens=192,
                temperature=0.2,
                event_timeout_seconds=0.05,
                full_timeout_seconds=1.0,
            )
    finally:
        client.close()

    assert request_count == 1


def test_streamed_analysis_full_deadline_aborts_without_retry() -> None:
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            text=(
                'data: {"model":"vendor/model-actual",'
                '"choices":[{"delta":{"content":"核对：类型=击杀\\n"}}]}\n\n'
                'data: {"model":"vendor/model-actual",'
                '"choices":[{"delta":{"content":"事件：击杀\\n"}}]}\n\n'
                'data: {"model":"vendor/model-actual",'
                '"choices":[{"delta":{"content":"场面：描述。描述。"}}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    client = OpenRouterClient(
        "test-api-key",
        transport=httpx.MockTransport(handler),
        clock=_StepClock(step=0.1),
    )
    try:
        with pytest.raises(LlmError, match="场面描述超时") as caught:
            client.analyze_stream(
                model="vendor/model-under-test",
                system_prompt="system",
                user_prompt="card",
                max_tokens=192,
                temperature=0.2,
                event_timeout_seconds=0.4,
                full_timeout_seconds=0.45,
            )
    finally:
        client.close()

    assert request_count == 1
    assert caught.value.partial_event_text == "击杀"
    assert caught.value.event_latency_seconds == pytest.approx(0.3)


def test_streamed_analysis_surfaces_midstream_error_without_retry() -> None:
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            text=(
                'data: {"model":"vendor/model-actual",'
                '"choices":[{"delta":{"content":"核对：类型=击杀\\n"}}]}\n\n'
                'data: {"model":"vendor/model-actual",'
                '"choices":[{"delta":{"content":"事件：击杀\\n"}}]}\n\n'
                'data: {"error":{"message":"provider disconnected"},'
                '"choices":[{"delta":{},"finish_reason":"error"}]}\n\n'
            ),
        )

    client = OpenRouterClient(
        "test-api-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(LlmError, match="provider disconnected"):
            client.analyze_stream(
                model="vendor/model-under-test",
                system_prompt="system",
                user_prompt="card",
                max_tokens=192,
                temperature=0.2,
                event_timeout_seconds=3.0,
                full_timeout_seconds=6.0,
            )
    finally:
        client.close()

    assert request_count == 1


@pytest.mark.parametrize(
    "text",
    (
        "只有一行",
        "核对：类型=击杀\n事件：击杀",
        "核对：类型=击杀\n事件：\n场面：描述",
        "核对：类型=击杀\n事件：击杀\n场面：描述\n多余：内容",
    ),
)
def test_analysis_protocol_rejects_malformed_output(text: str) -> None:
    with pytest.raises(LlmError):
        parse_analysis_text(text)
