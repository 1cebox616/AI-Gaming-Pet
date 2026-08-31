"""Live-model commentary never uses the network in unit tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from pet.core.adapter_api import SpeechRequest
from pet.core.config import LlmConfig, LlmProfileConfig
from pet.core.lines import Utterance
from pet.core.llm import LlmCooldownError, LlmError, LlmResult, LlmUsage
from pet.core.speaker import OnlineCommentaryRuntime, _Request


@dataclass
class _Bridge:
    utterances: list[Utterance] = field(default_factory=list)
    state_updates: int = 0

    async def broadcast_commentary(self, utterance: Utterance) -> None:
        self.utterances.append(utterance)

    async def publish_runtime_state(self) -> None:
        self.state_updates += 1


class _Client:
    def __init__(self, result: LlmResult | Exception) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    def complete(self, **kwargs: object) -> LlmResult:
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _speech(
    fact_text: str = "【过程】玩家阵亡\n【场景标签】无",
    *,
    llm_profile: str | None = None,
) -> SpeechRequest:
    return SpeechRequest(
        request_id="event-1",
        game_id="cs2",
        fact_text=fact_text,
        urgency=45,
        interrupt=True,
        supersedes_request_id=None,
        vocabulary_id="cs2",
        llm_profile=llm_profile,
        fallback_text="模板兜底",
        fallback_emotion="neutral",
        ts=1.0,
    )


def _request() -> _Request:
    return _Request(0, _speech())


def _config() -> LlmConfig:
    return LlmConfig(enabled=True, model="test/model", provider="")


def _result(text: str = "这波寄了") -> LlmResult:
    return LlmResult(
        text=text,
        usage=LlmUsage(prompt_tokens=10, completion_tokens=4, cost_usd=0.001),
        latency_seconds=0.1,
        model="test/model",
        provider=None,
    )


def test_disabled_configuration_uses_template_without_constructing_client() -> None:
    async def exercise() -> None:
        bridge = _Bridge()
        runtime = OnlineCommentaryRuntime(
            LlmConfig(enabled=False),
            bridge,  # type: ignore[arg-type]
            client_factory=lambda _: (_ for _ in ()).throw(AssertionError("must not build")),
        )
        await runtime.start()
        await runtime._fallback(_speech())
        assert runtime.state().mode == "template"
        assert runtime.state().reason == "未启用"
        assert bridge.utterances[0].id == "template-event-1"

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ("请求超时", "HTTP 503", "网络请求失败"))
def test_transport_failures_fall_back_to_template_and_pass_explicit_reasoning_none(
    failure: str,
) -> None:
    async def exercise() -> None:
        bridge = _Bridge()
        client = _Client(LlmError(failure))
        runtime = OnlineCommentaryRuntime(
            _config(), bridge, client_factory=lambda _: client  # type: ignore[arg-type]
        )
        await runtime.start()
        await runtime._complete(_request())
        assert bridge.utterances[-1].id == "template-event-1"
        assert runtime.state().consecutive_failures == 1
        assert client.calls[0]["reasoning_effort"] == "none"
        await runtime.shutdown()

    asyncio.run(exercise())


def test_missing_environment_client_falls_back_to_template_mode() -> None:
    async def exercise() -> None:
        bridge = _Bridge()
        runtime = OnlineCommentaryRuntime(
            _config(),
            bridge,  # type: ignore[arg-type]
            client_factory=lambda _: (_ for _ in ()).throw(LlmError("环境变量缺失")),
        )
        await runtime.start()
        assert runtime.state().mode == "template"
        assert runtime.state().reason == "环境变量缺失"
        await runtime._fallback(_speech())
        assert bridge.utterances[-1].id == "template-event-1"

    asyncio.run(exercise())


def test_empty_response_and_hard_gate_each_fall_back() -> None:
    async def exercise() -> None:
        for text in ("", "队友帮你补了"):
            bridge = _Bridge()
            client = _Client(_result(text))
            runtime = OnlineCommentaryRuntime(
                _config(), bridge, client_factory=lambda _: client  # type: ignore[arg-type]
            )
            await runtime.start()
            await runtime._complete(_request())
            assert bridge.utterances[-1].id == "template-event-1"
            assert runtime.state().consecutive_failures == 1
            await runtime.shutdown()

    asyncio.run(exercise())


def test_heavy_fire_phrase_without_misfire_death_fact_falls_back() -> None:
    async def exercise() -> None:
        bridge = _Bridge()
        client = _Client(_result("开了这么多枪没打死一个"))
        runtime = OnlineCommentaryRuntime(
            _config(), bridge, client_factory=lambda _: client  # type: ignore[arg-type]
        )
        await runtime.start()
        request = _Request(
            0,
            _speech(
                "【事件】阵亡\n【过程】玩家阵亡，开火后没打过\n【场景标签】对枪输了"
            ),
        )

        await runtime._complete(request)

        assert bridge.utterances[-1].id == "template-event-1"
        assert runtime.state().consecutive_failures == 1
        await runtime.shutdown()

    asyncio.run(exercise())


def test_three_failures_hold_template_mode_until_next_match() -> None:
    async def exercise() -> None:
        bridge = _Bridge()
        client = _Client(LlmError("timeout"))
        runtime = OnlineCommentaryRuntime(
            _config(), bridge, client_factory=lambda _: client  # type: ignore[arg-type]
        )
        await runtime.start()
        for _ in range(3):
            await runtime._complete(_request())
        assert runtime.state().mode == "template"
        assert runtime.state().reason == "连续失败"
        await runtime.reset_match()
        assert runtime.state().mode == "ai"
        assert runtime.state().consecutive_failures == 0
        await runtime.shutdown()

    asyncio.run(exercise())


def test_cooldown_drop_uses_template_without_counting_a_call_or_failure() -> None:
    async def exercise() -> None:
        bridge = _Bridge()
        client = _Client(
            LlmCooldownError(
                "profile cooling down",
                cooldown_drop=True,
                cooldown_remaining_seconds=3.0,
                request_id="req-trigger",
            )
        )
        runtime = OnlineCommentaryRuntime(
            _config(), bridge, client_factory=lambda _: client  # type: ignore[arg-type]
        )
        await runtime.start()
        await runtime._complete(_request())

        state = runtime.state()
        assert bridge.utterances[-1].id == "template-event-1"
        assert state.call_count == 0
        assert state.consecutive_failures == 0
        assert state.mode == "ai"
        assert state.last_error is not None
        assert state.last_error["cooldown_drop"] is True
        assert state.last_error["request_id"] == "req-trigger"
        await runtime.shutdown()

    asyncio.run(exercise())


def test_success_accumulates_reported_cost_and_broadcasts_model_line() -> None:
    async def exercise() -> None:
        bridge = _Bridge()
        client = _Client(_result())
        runtime = OnlineCommentaryRuntime(
            _config(), bridge, client_factory=lambda _: client  # type: ignore[arg-type]
        )
        await runtime.start()
        await runtime._complete(_request())
        assert bridge.utterances[-1].id == "llm-event-1"
        assert runtime.state().call_count == 1
        assert runtime.state().cost_usd == 0.001
        await runtime.shutdown()

    asyncio.run(exercise())


def test_named_profile_overrides_only_declared_model_settings() -> None:
    async def exercise() -> None:
        bridge = _Bridge()
        client = _Client(_result())
        timeouts: list[float] = []

        def create_client(timeout_seconds: float) -> _Client:
            timeouts.append(timeout_seconds)
            return client

        configuration = _config().model_copy(
            update={
                "profiles": {
                    "fast": LlmProfileConfig(
                        model="profile/model",
                        timeout_seconds=1.5,
                        max_tokens=64,
                    )
                }
            }
        )
        runtime = OnlineCommentaryRuntime(
            configuration,
            bridge,  # type: ignore[arg-type]
            client_factory=create_client,
        )
        await runtime.start()
        await runtime._complete(_Request(0, _speech(llm_profile="fast")))

        assert client.calls[-1]["model"] == "profile/model"
        assert client.calls[-1]["max_tokens"] == 64
        assert client.calls[-1]["temperature"] == 0.9
        assert timeouts == [3.0, 1.5]
        await runtime.shutdown()

    asyncio.run(exercise())
