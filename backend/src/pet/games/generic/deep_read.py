"""Reusable asynchronous primitive for off-path deep vision reads."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pet.core.config import LlmConfig
from pet.core.llm import (
    LlmDispatchStats,
    LlmImage,
    LlmResult,
    LlmVisionClientProtocol,
)


@dataclass(frozen=True, slots=True)
class DeepReadRequest:
    system_prompt: str
    user_prompt: str
    images: Sequence[LlmImage]


@dataclass(frozen=True, slots=True)
class DeepReadResult:
    result: LlmResult
    cost_usd: float


class DeepVisionReader:
    """Run one non-streaming multimodal call off the event-loop thread."""

    def __init__(
        self,
        client: LlmVisionClientProtocol,
        configuration: LlmConfig,
        *,
        input_price_per_million_usd: float | None,
        output_price_per_million_usd: float | None,
        reasoning_effort: Literal[
            "none", "minimal", "low", "medium", "high"
        ] = "high",
    ) -> None:
        if not configuration.enabled or not configuration.model.strip():
            raise ValueError("deep vision profile must be enabled and name a model")
        self._client = client
        self._configuration = configuration
        self._input_price = input_price_per_million_usd
        self._output_price = output_price_per_million_usd
        self._reasoning_effort = reasoning_effort

    async def read(self, request: DeepReadRequest) -> DeepReadResult:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                self._client.complete_with_images,
                model=self._configuration.model,
                provider=self._configuration.provider or None,
                system_prompt=request.system_prompt,
                user_prompt=request.user_prompt,
                images=request.images,
                max_image_edge=None,
                max_tokens=self._configuration.max_tokens,
                temperature=self._configuration.temperature,
                reasoning_effort=self._reasoning_effort,
            ),
            timeout=self._configuration.timeout_seconds,
        )
        return DeepReadResult(result=result, cost_usd=self._price(result))

    def close(self) -> None:
        close_client = getattr(self._client, "close", None)
        if callable(close_client):
            close_client()

    def dispatch_stats(self) -> LlmDispatchStats | None:
        snapshot = getattr(self._client, "dispatch_stats", None)
        return snapshot() if callable(snapshot) else None

    def _price(self, result: LlmResult) -> float:
        if result.usage.cost_usd is not None:
            return result.usage.cost_usd
        if self._input_price is None or self._output_price is None:
            raise ValueError("deep vision response omitted cost and profile has no prices")
        prompt = result.usage.prompt_tokens or 0
        completion = result.usage.completion_tokens or 0
        return (
            prompt * self._input_price + completion * self._output_price
        ) / 1_000_000.0
