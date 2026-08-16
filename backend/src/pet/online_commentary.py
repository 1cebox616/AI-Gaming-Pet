"""Non-blocking live LLM commentary with deterministic template fallback."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging

from pet.bridge import LlmRuntimeStateMessage, PetBridge
from pet.commentary import CommentaryGenerator
from pet.config import LlmConfig
from pet.event_card import render_fact_sentence
from pet.events import GameEvent
from pet.gsi import GameSnapshot
from pet.lines import Utterance
from pet.llm import LlmClientProtocol, LlmError, LlmResult, OpenRouterClient
from pet.prompt import load_system_prompt
from pet.session import GameState
from pet.situation import RoundSituation
from pet.style_review import check_hard_violations

logger = logging.getLogger(__name__)

# Three failures distinguish a transient bad response from an unavailable service,
# while avoiding a three-second delay on every subsequent event in the same match.
MAX_CONSECUTIVE_LLM_FAILURES = 3


@dataclass(frozen=True, slots=True)
class _Request:
    generation: int
    event: GameEvent
    map_name: str | None
    fact_sentence: str


class OnlineCommentaryRuntime:
    """Queue model work after policy selection; never await it on the GSI path."""

    def __init__(
        self,
        configuration: LlmConfig,
        bridge: PetBridge,
        generator: CommentaryGenerator,
        *,
        client_factory: Callable[[float], LlmClientProtocol] | None = None,
    ) -> None:
        self._configuration = configuration
        self._bridge = bridge
        self._generator = generator
        self._client_factory = client_factory or (
            lambda timeout_seconds: OpenRouterClient.from_env(
                timeout_seconds=timeout_seconds
            )
        )
        self._client: LlmClientProtocol | None = None
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

    async def reset_match(self) -> None:
        """Permit another attempt next match and prevent old work from speaking late."""
        self._generation += 1
        self._consecutive_failures = 0
        if self._client is not None:
            self._mode = "ai"
            self._reason = ""
        await self._bridge.publish_runtime_state()

    async def submit(
        self,
        snapshot: GameSnapshot,
        game: GameState,
        round_situation: RoundSituation,
        event: GameEvent,
    ) -> None:
        """Render immutable facts now, then enqueue model work or use the template."""
        if self._mode != "ai" or self._queue is None:
            await self._fallback(event, snapshot.map_name)
            return
        self._queue.put_nowait(
            _Request(
                generation=self._generation,
                event=event,
                map_name=snapshot.map_name,
                fact_sentence=render_fact_sentence(snapshot, game, round_situation, event),
            )
        )

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            request = await self._queue.get()
            if request is None:
                return
            await self._complete(request)

    async def _complete(self, request: _Request) -> None:
        client = self._client
        if client is None or request.generation != self._generation:
            return
        self._call_count += 1
        try:
            result = await asyncio.to_thread(
                client.complete,
                model=self._configuration.model,
                provider=self._configuration.provider or None,
                system_prompt=load_system_prompt("inference", max_chars=30),
                user_prompt=request.fact_sentence,
                max_tokens=self._configuration.max_tokens,
                temperature=self._configuration.temperature,
                reasoning_effort="none",
            )
            text = _validated_text(result, request.fact_sentence)
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
            Utterance(id=f"llm-{request.event.id}", text=text, emotion="neutral")
        )
        await self._bridge.publish_runtime_state()

    async def _failed(self, request: _Request, reason: str) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= MAX_CONSECUTIVE_LLM_FAILURES:
            self._mode = "template"
            self._reason = "连续失败"
        logger.warning("live LLM output discarded; using template: %s", reason)
        await self._fallback(request.event, request.map_name)
        await self._bridge.publish_runtime_state()

    async def _fallback(self, event: GameEvent, map_name: str | None) -> None:
        await self._bridge.broadcast_commentary(
            self._generator.generate(event, map_name=map_name)
        )


def _configuration_reason(configuration: LlmConfig) -> str:
    if not configuration.enabled:
        return "未启用"
    if not configuration.model.strip():
        return "型号未配置"
    return ""


def _validated_text(result: LlmResult, fact_sentence: str) -> str:
    text = result.text.strip()
    if not text:
        raise ValueError("模型返回为空")
    checks = check_hard_violations(text, fact_sentence=fact_sentence)
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
