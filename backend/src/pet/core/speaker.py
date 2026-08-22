"""Non-blocking live LLM commentary with deterministic template fallback."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging

from pet.core.adapter_api import SpeechRequest
from pet.core.bridge import LlmRuntimeStateMessage, PetBridge
from pet.core.config import LlmConfig
from pet.core.gate import check_hard_violations
from pet.core.lines import Utterance
from pet.core.llm import LlmClientProtocol, LlmError, LlmResult, OpenRouterClient
from pet.core.prompt import load_system_prompt

logger = logging.getLogger(__name__)

# Three failures distinguish a transient bad response from an unavailable service,
# while avoiding a three-second delay on every subsequent event in the same match.
MAX_CONSECUTIVE_LLM_FAILURES = 3


@dataclass(frozen=True, slots=True)
class _Request:
    generation: int
    speech: SpeechRequest


class OnlineCommentaryRuntime:
    """Queue model work after policy selection; never await it on the GSI path."""

    def __init__(
        self,
        configuration: LlmConfig,
        bridge: PetBridge,
        *,
        client_factory: Callable[[float], LlmClientProtocol] | None = None,
    ) -> None:
        self._configuration = configuration
        self._bridge = bridge
        self._client_factory = client_factory or (
            lambda timeout_seconds: OpenRouterClient.from_env(
                timeout_seconds=timeout_seconds
            )
        )
        self._client: LlmClientProtocol | None = None
        self._profile_clients: dict[str, LlmClientProtocol] = {}
        self._queue: asyncio.Queue[_Request | None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._generation = 0
        self._consecutive_failures = 0
        self._call_count = 0
        self._cost_usd = 0.0
        self._cost_available = True
        self._mode = "template"
        self._reason = _configuration_reason(configuration)

    def state(self) -> LlmRuntimeStateMessage:
        """Return a transport-safe snapshot for the existing state message."""
        return LlmRuntimeStateMessage(
            mode=self._mode,  # type: ignore[arg-type]
            reason=self._reason,
            consecutive_failures=self._consecutive_failures,
            call_count=self._call_count,
            cost_usd=self._cost_usd if self._cost_available else None,
        )

    async def start(self) -> None:
        """Start one worker only when configuration and credentials allow AI mode."""
        if self._reason:
            logger.info("live LLM disabled; using templates: %s", self._reason)
            return
        try:
            self._client = self._client_factory(self._configuration.timeout_seconds)
        except LlmError as error:
            self._reason = "环境变量缺失"
            logger.info("live LLM unavailable; using templates: %s", error)
            return
        self._mode = "ai"
        self._reason = ""
        self._queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._run(), name="pet-live-llm")
        await self._bridge.publish_runtime_state()

    async def shutdown(self) -> None:
        """Stop accepting queued work and close the optional HTTP client."""
        if self._queue is not None and self._worker is not None:
            self._queue.put_nowait(None)
            await self._worker
        self._queue = None
        self._worker = None
        close = getattr(self._client, "close", None)
        if callable(close):
            await asyncio.to_thread(close)
        self._client = None
        for profile_client in self._profile_clients.values():
            profile_close = getattr(profile_client, "close", None)
            if callable(profile_close):
                await asyncio.to_thread(profile_close)
        self._profile_clients.clear()

    async def reset_match(self) -> None:
        """Permit another attempt next match and prevent old work from speaking late."""
        self._generation += 1
        self._consecutive_failures = 0
        if self._client is not None:
            self._mode = "ai"
            self._reason = ""
        await self._bridge.publish_runtime_state()

    async def submit(self, request: SpeechRequest) -> None:
        """Enqueue one immutable adapter request or speak its template fallback."""
        if self._mode != "ai" or self._queue is None:
            await self._fallback(request)
            return
        self._queue.put_nowait(_Request(generation=self._generation, speech=request))

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            request = await self._queue.get()
            if request is None:
                return
            await self._complete(request)

    async def _complete(self, request: _Request) -> None:
        if request.generation != self._generation:
            return
        configuration = _request_configuration(
            self._configuration, request.speech.llm_profile
        )
        if not configuration.enabled or not configuration.model.strip():
            await self._fallback(request.speech)
            return
        try:
            client = self._client_for(request.speech.llm_profile, configuration)
        except LlmError as error:
            await self._failed(request, str(error))
            return
        self._call_count += 1
        try:
            result = await asyncio.to_thread(
                client.complete,
                model=configuration.model,
                provider=configuration.provider or None,
                system_prompt=load_system_prompt(
                    request.speech.vocabulary_id, max_chars=30
                ),
                user_prompt=request.speech.fact_text,
                max_tokens=configuration.max_tokens,
                temperature=configuration.temperature,
                reasoning_effort="none",
            )
            text = _validated_text(result, request.speech)
        except (LlmError, ValueError) as error:
            if request.generation == self._generation:
                await self._failed(request, str(error))
            return
        except Exception as error:  # Keep a malformed client from silencing the pet.
            if request.generation == self._generation:
                await self._failed(request, f"unexpected model error: {error}")
            return

        if request.generation != self._generation:
            return
        if result.usage.cost_usd is None:
            self._cost_available = False
        else:
            self._cost_usd += result.usage.cost_usd
        self._consecutive_failures = 0
        await self._bridge.broadcast_commentary(
            Utterance(
                id=f"llm-{request.speech.request_id}", text=text, emotion="neutral"
            )
        )
        await self._bridge.publish_runtime_state()

    def _client_for(
        self, profile_id: str | None, configuration: LlmConfig
    ) -> LlmClientProtocol:
        if profile_id is None or profile_id not in self._configuration.profiles:
            if self._client is None:
                raise LlmError("模型客户端不可用")
            return self._client
        client = self._profile_clients.get(profile_id)
        if client is None:
            client = self._client_factory(configuration.timeout_seconds)
            self._profile_clients[profile_id] = client
        return client

    async def _failed(self, request: _Request, reason: str) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= MAX_CONSECUTIVE_LLM_FAILURES:
            self._mode = "template"
            self._reason = "连续失败"
        logger.warning("live LLM output discarded; using template: %s", reason)
        await self._fallback(request.speech)
        await self._bridge.publish_runtime_state()

    async def _fallback(self, request: SpeechRequest) -> None:
        if request.fallback_text is None:
            return
        await self._bridge.broadcast_commentary(
            Utterance(
                id=f"template-{request.request_id}",
                text=request.fallback_text,
                emotion=request.fallback_emotion or "neutral",
            )
        )


def _configuration_reason(configuration: LlmConfig) -> str:
    if not configuration.enabled:
        return "未启用"
    if not configuration.model.strip():
        return "型号未配置"
    return ""


def _request_configuration(
    configuration: LlmConfig, profile_id: str | None
) -> LlmConfig:
    if profile_id is None:
        return configuration
    profile = configuration.profiles.get(profile_id)
    if profile is None:
        logger.warning("unknown LLM profile %r; using the default profile", profile_id)
        return configuration
    return configuration.model_copy(update=profile.model_dump(exclude_none=True))


def _validated_text(result: LlmResult, request: SpeechRequest) -> str:
    text = result.text.strip()
    if not text:
        raise ValueError("模型返回为空")
    checks = check_hard_violations(
        text,
        fact_sentence=request.fact_text,
        vocabulary_id=request.vocabulary_id,
    )
    if checks.exceeds_30_chars:
        raise ValueError(f"模型输出超过 30 个汉字；原文={text}")
    if checks.unsupported_terms:
        raise ValueError(f"无依据词：{'、'.join(checks.unsupported_terms)}；原文={text}")
    if checks.binding_violations:
        raise ValueError(f"用词绑定：{'、'.join(checks.binding_violations)}；原文={text}")
    if checks.economy_tier_rewrite:
        raise ValueError(f"经济档位改写；原文={text}")
    if checks.eco_called_pistol_round:
        raise ValueError(f"把 eco 局说成手枪局；原文={text}")
    return text
