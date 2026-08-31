"""Provider-neutral language-model result types and an OpenRouter client."""

from __future__ import annotations

import argparse
import base64
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlparse

import httpx
from PIL import Image

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Conservative placeholders, not measured provider limits. A valid
# Retry-After always wins; headerless 429s back off from one second to a
# capped minute until a successful response resets the exponent.
RATE_LIMIT_BACKOFF_BASE_SECONDS = 1.0
RATE_LIMIT_BACKOFF_MAX_SECONDS = 60.0

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LlmUsage:
    """One call's token accounting as reported by the upstream."""

    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd: float | None
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class LlmResult:
    """The text, accounting, and wall-clock cost of one completed call."""

    text: str
    usage: LlmUsage
    latency_seconds: float
    model: str
    provider: str | None
    finish_reason: str | None = None
    ttft_seconds: float | None = None
    streamed: bool = False


@dataclass(frozen=True, slots=True)
class LlmModelEndpoint:
    """One live catalog entry used by offline evaluation tooling."""

    name: str
    provider: str
    prompt_price_per_token: float
    completion_price_per_token: float
    context_length: int | None


@dataclass(frozen=True, slots=True)
class LlmImage:
    """One image attachment and its human-readable position label.

    Captured production frames may stay entirely in memory; evaluation tools
    retain the existing file-backed path.
    """

    path: Path | Image.Image
    label: str
    max_edge: int | None = None
    target_width: int | None = None
    encoding: Literal["png", "jpeg"] = "png"
    jpeg_quality: int = 85


@dataclass(frozen=True, slots=True)
class LlmImageUploadMetadata:
    """Pixel dimensions and PNG payload size produced by local preprocessing."""

    width: int
    height: int
    byte_size: int


@dataclass(frozen=True, slots=True)
class LlmAnalysisResult:
    """A streamed self-audit, event line, and factual scene description."""

    audit_text: str
    event_text: str
    scene_text: str
    usage: LlmUsage
    event_latency_seconds: float
    latency_seconds: float
    model: str
    provider: str | None


ReasoningParameterMode = Literal["omitted", "effort_none", "enabled_false"]


@dataclass(frozen=True, slots=True)
class ReasoningProbeAttempt:
    """One measured reasoning-parameter spelling attempted by the probe."""

    mode: ReasoningParameterMode
    accepted: bool
    reasoning_tokens: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class LlmProbeResult:
    """Connectivity evidence required before a custom-endpoint exam."""

    profile_name: str
    environment_variable: str
    environment_is_set: bool
    model_list_status: str
    model_available: bool
    text_available: bool
    image_available: bool
    streaming_available: bool
    ttft_ms: float | None
    latency_ms: float | None
    reasoning_attempts: tuple[ReasoningProbeAttempt, ...]
    selected_reasoning_mode: ReasoningParameterMode | None
    error: str | None

    @property
    def passed(self) -> bool:
        return (
            self.environment_is_set
            and self.model_available
            and self.text_available
            and self.image_available
            and self.streaming_available
            and self.error is None
        )


@dataclass(frozen=True, slots=True)
class LlmDispatchStats:
    """One profile's rate-limit and cooldown accounting snapshot."""

    profile_name: str
    rate_limit_count: int
    cooldown_seconds: float
    cooldown_drop_count: int
    cooling_down: bool
    cooldown_remaining_seconds: float


class LlmError(Exception):
    """One failed call, carrying enough context to appear in a report."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        latency_seconds: float | None = None,
        partial_event_text: str | None = None,
        event_latency_seconds: float | None = None,
        provider: str | None = None,
        profile_name: str | None = None,
        error_type: str | None = None,
        provider_code: str | int | None = None,
        retry_after: str | None = None,
        retry_after_seconds: float | None = None,
        request_id: str | None = None,
        request_id_header: str | None = None,
        occurred_at: str | None = None,
        cooldown_drop: bool = False,
        cooldown_remaining_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.latency_seconds = latency_seconds
        self.partial_event_text = partial_event_text
        self.event_latency_seconds = event_latency_seconds
        self.provider = provider
        self.profile_name = profile_name
        self.error_type = error_type
        self.provider_code = provider_code
        self.retry_after = retry_after
        self.retry_after_seconds = retry_after_seconds
        self.request_id = request_id
        self.request_id_header = request_id_header
        self.occurred_at = occurred_at or datetime.now(timezone.utc).isoformat()
        self.cooldown_drop = cooldown_drop
        self.cooldown_remaining_seconds = cooldown_remaining_seconds

    def metadata(self) -> dict[str, object]:
        """Return report-safe structured failure context without the prompt."""
        return {
            "message": super().__str__(),
            "status_code": self.status_code,
            "error_type": self.error_type,
            "provider_code": self.provider_code,
            "retry_after": self.retry_after,
            "retry_after_seconds": self.retry_after_seconds,
            "request_id": self.request_id,
            "request_id_header": self.request_id_header,
            "occurred_at": self.occurred_at,
            "provider": self.provider,
            "profile_name": self.profile_name,
            "latency_seconds": self.latency_seconds,
            "cooldown_drop": self.cooldown_drop,
            "cooldown_remaining_seconds": self.cooldown_remaining_seconds,
        }

    def diagnostic(self) -> str:
        """Render metadata for logs and human-readable evaluation reports."""
        fields = [
            ("status", self.status_code),
            ("error_type", self.error_type),
            ("provider_code", self.provider_code),
            ("retry_after", self.retry_after),
            ("retry_after_seconds", self.retry_after_seconds),
            ("request_id", self.request_id),
            ("request_id_header", self.request_id_header),
            ("occurred_at", self.occurred_at),
            ("provider", self.provider),
            ("profile_name", self.profile_name),
            ("cooldown_drop", True if self.cooldown_drop else None),
            ("cooldown_remaining_seconds", self.cooldown_remaining_seconds),
        ]
        suffix = " ".join(
            f"{key}={value}" for key, value in fields if value is not None
        )
        return f"{super().__str__()} [{suffix}]" if suffix else super().__str__()


class LlmCooldownError(LlmError):
    """A request discarded locally before any HTTP dispatch."""


class LlmStreamingUnsupported(LlmError):
    """The selected upstream explicitly cannot return this request as SSE."""


class _RateLimitCooldown:
    """Thread-safe, per-client dispatch gate for one configured profile."""

    def __init__(
        self,
        profile_name: str,
        *,
        clock: Callable[[], float],
        backoff_base_seconds: float,
        backoff_max_seconds: float,
    ) -> None:
        if backoff_base_seconds <= 0 or backoff_max_seconds <= 0:
            raise ValueError("rate-limit backoff bounds must be positive")
        if backoff_max_seconds < backoff_base_seconds:
            raise ValueError("rate-limit backoff cap must not be below its base")
        self._profile_name = profile_name
        self._clock = clock
        self._base = backoff_base_seconds
        self._cap = backoff_max_seconds
        self._lock = threading.Lock()
        self._rate_limit_count = 0
        self._cooldown_drop_count = 0
        self._cooldown_seconds_total = 0.0
        self._active_started_at: float | None = None
        self._cooldown_until = 0.0
        self._consecutive_rate_limits = 0
        self._last_limit_error: LlmError | None = None

    def before_dispatch(self) -> None:
        """Reject a request locally when this profile is still cooling down."""
        now = self._clock()
        with self._lock:
            self._finish_expired_locked(now)
            if self._active_started_at is None:
                return
            remaining = max(0.0, self._cooldown_until - now)
            self._cooldown_drop_count += 1
            trigger = self._last_limit_error
        logger.debug(
            "模型档位 %s 仍在限流冷却，直接丢弃请求；remaining=%.3fs",
            self._profile_name,
            remaining,
        )
        raise LlmCooldownError(
            f"模型档位 {self._profile_name} 限流冷却中，派发前丢弃请求",
            profile_name=self._profile_name,
            provider=trigger.provider if trigger is not None else None,
            error_type=trigger.error_type if trigger is not None else None,
            provider_code=trigger.provider_code if trigger is not None else None,
            retry_after=trigger.retry_after if trigger is not None else None,
            retry_after_seconds=(
                trigger.retry_after_seconds if trigger is not None else None
            ),
            request_id=trigger.request_id if trigger is not None else None,
            request_id_header=(
                trigger.request_id_header if trigger is not None else None
            ),
            cooldown_drop=True,
            cooldown_remaining_seconds=remaining,
        )

    def enter(self, error: LlmError) -> None:
        """Record one 429 and open or extend the profile cooldown interval."""
        now = self._clock()
        with self._lock:
            self._finish_expired_locked(now)
            self._rate_limit_count += 1
            if error.retry_after_seconds is not None:
                duration = max(0.0, error.retry_after_seconds)
                source = "Retry-After"
            else:
                duration = min(
                    self._base * (2 ** min(self._consecutive_rate_limits, 30)),
                    self._cap,
                )
                source = "exponential-placeholder"
            self._consecutive_rate_limits += 1
            was_active = self._active_started_at is not None
            if not was_active:
                self._active_started_at = now
                self._cooldown_until = now + duration
                self._cooldown_seconds_total += duration
            else:
                previous_until = self._cooldown_until
                self._cooldown_until = max(previous_until, now + duration)
                self._cooldown_seconds_total += max(
                    0.0, self._cooldown_until - previous_until
                )
            self._last_limit_error = error
            until = self._cooldown_until
        logger.warning(
            "模型档位 %s 收到限流，%s冷却 %.3fs 至 monotonic=%.3f；依据=%s；%s",
            self._profile_name,
            "延长" if was_active else "进入",
            duration,
            until,
            source,
            error.diagnostic(),
        )

    def record_success(self) -> None:
        """Reset only the headerless backoff exponent after a real success."""
        now = self._clock()
        with self._lock:
            self._finish_expired_locked(now)
            self._consecutive_rate_limits = 0

    def snapshot(self) -> LlmDispatchStats:
        now = self._clock()
        with self._lock:
            self._finish_expired_locked(now)
            active = self._active_started_at is not None
            remaining = max(0.0, self._cooldown_until - now) if active else 0.0
            return LlmDispatchStats(
                profile_name=self._profile_name,
                rate_limit_count=self._rate_limit_count,
                cooldown_seconds=self._cooldown_seconds_total,
                cooldown_drop_count=self._cooldown_drop_count,
                cooling_down=active,
                cooldown_remaining_seconds=remaining,
            )

    def _finish_expired_locked(self, now: float) -> None:
        if self._active_started_at is None or now < self._cooldown_until:
            return
        elapsed = max(0.0, self._cooldown_until - self._active_started_at)
        self._active_started_at = None
        self._cooldown_until = 0.0
        logger.info(
            "模型档位 %s 限流冷却结束；本段 %.3fs，累计 %.3fs，自然恢复派发",
            self._profile_name,
            elapsed,
            self._cooldown_seconds_total,
        )


class LlmClientProtocol(Protocol):
    """The synchronous completion boundary used by offline evaluation."""

    def complete(
        self,
        *,
        model: str,
        provider: str | None = None,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        seed: int | None = None,
        reasoning_effort: str | None = None,
        reasoning_enabled: bool | None = None,
    ) -> LlmResult:
        """Return one completed response without retrying."""
        ...


class LlmAnalysisClientProtocol(Protocol):
    """The synchronous streamed-analysis boundary used by offline evaluation."""

    def analyze_stream(
        self,
        *,
        model: str,
        provider: str | None = None,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        event_timeout_seconds: float,
        full_timeout_seconds: float,
        seed: int | None = None,
        reasoning_effort: str = "none",
    ) -> LlmAnalysisResult:
        """Return the first event line and complete scene without retrying."""
        ...


class LlmVisionClientProtocol(Protocol):
    """The synchronous image-completion boundary used only by offline tools."""

    def complete_with_images(
        self,
        *,
        model: str,
        provider: str | None = None,
        system_prompt: str,
        user_prompt: str,
        images: Sequence[LlmImage],
        max_image_edge: int | None,
        max_tokens: int,
        temperature: float,
        seed: int | None = None,
        reasoning_effort: str | None = None,
        reasoning_enabled: bool | None = None,
    ) -> LlmResult:
        """Return one completed response after locally encoding the images."""
        ...

    def complete_with_images_stream(
        self,
        *,
        model: str,
        provider: str | None = None,
        system_prompt: str,
        user_prompt: str,
        images: Sequence[LlmImage],
        max_image_edge: int | None,
        max_tokens: int,
        temperature: float,
        seed: int | None = None,
        reasoning_effort: str | None = None,
        reasoning_enabled: bool | None = None,
    ) -> LlmResult:
        """Stream one image response and report first visible-token latency."""
        ...


class OpenRouterClient:
    """Minimal client for non-streaming and streamed OpenRouter completions."""

    def __init__(
        self,
        api_key: str,
        *,
        profile_name: str = "default",
        base_url: str | None = None,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.perf_counter,
        rate_limit_backoff_base_seconds: float = RATE_LIMIT_BACKOFF_BASE_SECONDS,
        rate_limit_backoff_max_seconds: float = RATE_LIMIT_BACKOFF_MAX_SECONDS,
    ) -> None:
        if not api_key.strip():
            raise LlmError("模型端点 API 密钥为空")
        selected_base_url = base_url or OPENROUTER_BASE_URL
        # Provider routing is an extension of the default endpoint, not part of
        # the OpenAI-compatible contract. The presence of a configured base_url,
        # rather than any vendor/host name, is the only switch used here.
        self._allows_provider_routing = base_url is None
        self._endpoint_label = (
            "OpenRouter" if self._allows_provider_routing else "自定义模型端点"
        )
        parsed_url = urlparse(selected_base_url)
        self.endpoint_host = parsed_url.hostname or ""
        self._client = httpx.Client(
            base_url=f"{selected_base_url.rstrip('/')}/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )
        self._clock = clock
        self._profile_name = profile_name
        self._cooldown = _RateLimitCooldown(
            profile_name,
            clock=clock,
            backoff_base_seconds=rate_limit_backoff_base_seconds,
            backoff_max_seconds=rate_limit_backoff_max_seconds,
        )

    @classmethod
    def from_env(
        cls,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> OpenRouterClient:
        """Build a client from the sole supported credential source."""
        api_key = os.environ.get(OPENROUTER_API_KEY_ENV)
        if api_key is None or not api_key.strip():
            raise LlmError(
                f"未设置环境变量 {OPENROUTER_API_KEY_ENV}；无法调用 OpenRouter"
            )
        return cls(
            api_key,
            profile_name="default",
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )

    @classmethod
    def from_profile(
        cls,
        *,
        profile_name: str,
        base_url: str | None,
        api_key_env: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> OpenRouterClient:
        """Build one profile client from its exact credential variable."""
        api_key = os.environ.get(api_key_env)
        if api_key is None or not api_key.strip():
            raise LlmError(
                f"模型档位 {profile_name} 缺少环境变量 {api_key_env}；"
                "未回退到其他密钥或端点"
            )
        return cls(
            api_key,
            profile_name=profile_name,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )

    def complete(
        self,
        *,
        model: str,
        provider: str | None = None,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        seed: int | None = None,
        reasoning_effort: str | None = None,
        reasoning_enabled: bool | None = None,
    ) -> LlmResult:
        """Send one non-streaming chat completion and never retry failures."""
        self._cooldown.before_dispatch()
        return self._complete_messages(
            model=model,
            provider=provider,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
            seed=seed,
            reasoning_effort=reasoning_effort,
            reasoning_enabled=reasoning_enabled,
        )

    def list_model_ids(self) -> tuple[str, ...] | None:
        """Return advertised model IDs, or None when listing is unsupported."""
        try:
            response = self._client.get("models")
        except httpx.TimeoutException as error:
            raise LlmError(f"{self._endpoint_label}模型列表请求超时：{error}") from error
        except httpx.RequestError as error:
            raise LlmError(f"{self._endpoint_label}模型列表网络失败：{error}") from error
        if response.status_code in {404, 405, 501}:
            return None
        if not response.is_success:
            raise LlmError(
                _response_error_message(response, service_label=self._endpoint_label),
                status_code=response.status_code,
            )
        try:
            payload: object = response.json()
        except ValueError as error:
            raise LlmError(f"{self._endpoint_label}模型列表不是合法 JSON") from error
        if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
            raise LlmError(f"{self._endpoint_label}模型列表缺少 data 数组")
        return tuple(
            item["id"]
            for item in payload["data"]
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        )

    def list_model_endpoints(self, model: str) -> tuple[LlmModelEndpoint, ...] | None:
        """Return live provider and price entries when the endpoint advertises them."""
        try:
            response = self._client.get(f"models/{model}/endpoints")
        except httpx.TimeoutException as error:
            raise LlmError(f"{self._endpoint_label}模型端点目录请求超时：{error}") from error
        except httpx.RequestError as error:
            raise LlmError(f"{self._endpoint_label}模型端点目录网络失败：{error}") from error
        if response.status_code in {404, 405, 501}:
            return None
        if not response.is_success:
            raise LlmError(
                _response_error_message(response, service_label=self._endpoint_label),
                status_code=response.status_code,
            )
        try:
            payload: object = response.json()
        except ValueError as error:
            raise LlmError(f"{self._endpoint_label}模型端点目录不是合法 JSON") from error
        if not isinstance(payload, Mapping):
            raise LlmError(f"{self._endpoint_label}模型端点目录顶层不是对象")
        data = payload.get("data")
        endpoints = data.get("endpoints") if isinstance(data, Mapping) else None
        if not isinstance(endpoints, list):
            raise LlmError(f"{self._endpoint_label}模型端点目录缺少 data.endpoints 数组")
        parsed: list[LlmModelEndpoint] = []
        for item in endpoints:
            if not isinstance(item, Mapping):
                continue
            pricing = item.get("pricing")
            provider = item.get("provider_name")
            if not isinstance(pricing, Mapping) or not isinstance(provider, str):
                continue
            try:
                prompt_price = float(pricing["prompt"])
                completion_price = float(pricing["completion"])
            except (KeyError, TypeError, ValueError):
                continue
            name = item.get("name")
            context = item.get("context_length")
            parsed.append(
                LlmModelEndpoint(
                    name if isinstance(name, str) else "",
                    provider,
                    prompt_price,
                    completion_price,
                    context if isinstance(context, int) else None,
                )
            )
        return tuple(parsed)


    def complete_with_images(
        self,
        *,
        model: str,
        provider: str | None = None,
        system_prompt: str,
        user_prompt: str,
        images: Sequence[LlmImage],
        max_image_edge: int | None,
        max_tokens: int,
        temperature: float,
        seed: int | None = None,
        reasoning_effort: str | None = None,
        reasoning_enabled: bool | None = None,
    ) -> LlmResult:
        """Send text and local images as OpenAI-compatible content blocks."""
        self._cooldown.before_dispatch()
        messages = _image_messages(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=images,
            max_image_edge=max_image_edge,
        )
        return self._complete_messages(
            model=model,
            provider=provider,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            seed=seed,
            reasoning_effort=reasoning_effort,
            reasoning_enabled=reasoning_enabled,
        )

    def complete_with_images_stream(
        self,
        *,
        model: str,
        provider: str | None = None,
        system_prompt: str,
        user_prompt: str,
        images: Sequence[LlmImage],
        max_image_edge: int | None,
        max_tokens: int,
        temperature: float,
        seed: int | None = None,
        reasoning_effort: str | None = None,
        reasoning_enabled: bool | None = None,
    ) -> LlmResult:
        """Stream text for local images and measure the first visible token."""
        self._cooldown.before_dispatch()
        messages = _image_messages(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=images,
            max_image_edge=max_image_edge,
        )
        return self._stream_messages(
            model=model,
            provider=provider,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            seed=seed,
            reasoning_effort=reasoning_effort,
            reasoning_enabled=reasoning_enabled,
        )

    def _complete_messages(
        self,
        *,
        model: str,
        provider: str | None,
        messages: list[dict[str, object]],
        max_tokens: int,
        temperature: float,
        seed: int | None,
        reasoning_effort: str | None,
        reasoning_enabled: bool | None,
    ) -> LlmResult:
        """Send one prepared non-streaming message list without retrying."""
        started_at = self._clock()
        request_body: dict[str, object] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        effective_provider = provider if self._allows_provider_routing else None
        if effective_provider is not None:
            request_body["provider"] = {
                "only": [effective_provider],
                "allow_fallbacks": False,
            }
        if seed is not None:
            request_body["seed"] = seed
        if reasoning_effort is not None and reasoning_enabled is not None:
            raise ValueError("reasoning effort and enabled flag are mutually exclusive")
        if reasoning_effort is not None:
            if reasoning_effort not in {"none", "minimal", "low", "medium", "high"}:
                raise ValueError("unsupported reasoning effort")
            request_body["reasoning"] = {"effort": reasoning_effort}
        elif reasoning_enabled is not None:
            request_body["reasoning"] = {"enabled": reasoning_enabled}

        try:
            response = self._client.post(
                "chat/completions",
                json=request_body,
            )
        except httpx.TimeoutException as error:
            latency = self._clock() - started_at
            raise LlmError(
                f"{self._endpoint_label}请求超时：{error}", latency_seconds=latency
            ) from error
        except httpx.RequestError as error:
            latency = self._clock() - started_at
            raise LlmError(
                f"{self._endpoint_label}网络请求失败：{error}", latency_seconds=latency
            ) from error

        latency = self._clock() - started_at
        if not response.is_success:
            error = _response_llm_error(
                response,
                service_label=self._endpoint_label,
                latency_seconds=latency,
                fallback_provider=effective_provider,
                profile_name=self._profile_name,
            )
            if error.status_code == 429:
                self._cooldown.enter(error)
            raise error

        self._cooldown.record_success()

        try:
            payload: object = response.json()
            result = _parse_result(payload, latency_seconds=latency)
        except (TypeError, ValueError, KeyError, IndexError) as error:
            raise LlmError(
                f"{self._endpoint_label}返回了无法解析的成功响应：{error}",
                status_code=response.status_code,
                latency_seconds=latency,
            ) from error
        return result

    def _stream_messages(
        self,
        *,
        model: str,
        provider: str | None,
        messages: list[dict[str, object]],
        max_tokens: int,
        temperature: float,
        seed: int | None,
        reasoning_effort: str | None,
        reasoning_enabled: bool | None,
    ) -> LlmResult:
        """Stream one prepared message list without retrying."""
        started_at = self._clock()
        request_body: dict[str, object] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        effective_provider = provider if self._allows_provider_routing else None
        _apply_provider_and_reasoning(
            request_body,
            provider=effective_provider,
            seed=seed,
            reasoning_effort=reasoning_effort,
            reasoning_enabled=reasoning_enabled,
        )
        text_parts: list[str] = []
        ttft_seconds: float | None = None
        actual_model: str | None = None
        actual_provider: str | None = None
        finish_reason: str | None = None
        usage = LlmUsage(None, None, None)
        try:
            with self._client.stream(
                "POST", "chat/completions", json=request_body
            ) as response:
                if not response.is_success:
                    response.read()
                    message = _response_error_message(
                        response, service_label=self._endpoint_label
                    )
                    error_class = (
                        LlmStreamingUnsupported
                        if _streaming_is_unsupported(response, message)
                        else LlmError
                    )
                    raise _response_llm_error(
                        response,
                        service_label=self._endpoint_label,
                        latency_seconds=self._clock() - started_at,
                        fallback_provider=effective_provider,
                        profile_name=self._profile_name,
                        error_class=error_class,
                    )
                media_type = response.headers.get("content-type", "").lower()
                if media_type and "text/event-stream" not in media_type:
                    response.read()
                    raise LlmStreamingUnsupported(
                        f"{self._endpoint_label}未返回流式 SSE；改用非流式请求",
                        status_code=response.status_code,
                        latency_seconds=self._clock() - started_at,
                        provider=effective_provider,
                    )
                for line in response.iter_lines():
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        break
                    chunk = _parse_stream_chunk(data)
                    chunk_model = chunk.get("model")
                    if isinstance(chunk_model, str) and chunk_model.strip():
                        actual_model = chunk_model
                    chunk_provider = chunk.get("provider")
                    if isinstance(chunk_provider, str) and chunk_provider.strip():
                        actual_provider = chunk_provider
                    usage_value = chunk.get("usage")
                    if isinstance(usage_value, Mapping):
                        usage = _parse_usage(usage_value)
                    chunk_finish_reason = _stream_finish_reason(chunk)
                    if chunk_finish_reason is not None:
                        finish_reason = chunk_finish_reason
                    content = _stream_content(chunk)
                    if content:
                        if ttft_seconds is None:
                            ttft_seconds = self._clock() - started_at
                        text_parts.append(content)
        except LlmError as error:
            if error.status_code == 429:
                self._cooldown.enter(error)
            raise
        except httpx.TimeoutException as error:
            latency = self._clock() - started_at
            raise LlmError(
                f"{self._endpoint_label}流式请求超时：{error}",
                latency_seconds=latency,
                provider=actual_provider or effective_provider,
            ) from error
        except httpx.RequestError as error:
            latency = self._clock() - started_at
            raise LlmError(
                f"{self._endpoint_label}流式网络请求失败：{error}",
                latency_seconds=latency,
                provider=actual_provider or effective_provider,
            ) from error
        latency = self._clock() - started_at
        self._cooldown.record_success()
        if actual_model is None:
            raise LlmError(
                f"{self._endpoint_label}流式响应缺少实际型号 ID",
                latency_seconds=latency,
                provider=actual_provider or effective_provider,
            )
        return LlmResult(
            text="".join(text_parts).strip(),
            usage=usage,
            latency_seconds=latency,
            model=actual_model,
            provider=actual_provider or effective_provider,
            finish_reason=finish_reason,
            ttft_seconds=ttft_seconds,
            streamed=True,
        )

    def analyze_stream(
        self,
        *,
        model: str,
        provider: str | None = None,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        event_timeout_seconds: float,
        full_timeout_seconds: float,
        seed: int | None = None,
        reasoning_effort: str = "none",
    ) -> LlmAnalysisResult:
        """Stream one strict event/scene response and never retry failures."""
        if event_timeout_seconds <= 0:
            raise ValueError("event timeout must be positive")
        if full_timeout_seconds < event_timeout_seconds:
            raise ValueError("full timeout must be at least the event timeout")

        self._cooldown.before_dispatch()
        started_at = self._clock()
        if reasoning_effort not in {"none", "minimal", "low", "medium", "high"}:
            raise ValueError("unsupported reasoning effort")
        request_body: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "reasoning": {"effort": reasoning_effort},
        }
        effective_provider = provider if self._allows_provider_routing else None
        if effective_provider is not None:
            request_body["provider"] = {
                "only": [effective_provider],
                "allow_fallbacks": False,
            }
        if seed is not None:
            request_body["seed"] = seed

        text_parts: list[str] = []
        event_latency: float | None = None
        actual_model: str | None = None
        actual_provider: str | None = None
        usage = LlmUsage(None, None, None)
        try:
            with self._client.stream(
                "POST",
                "chat/completions",
                json=request_body,
                timeout=event_timeout_seconds,
            ) as response:
                if not response.is_success:
                    response.read()
                    raise _response_llm_error(
                        response,
                        service_label=self._endpoint_label,
                        latency_seconds=self._clock() - started_at,
                        fallback_provider=effective_provider,
                        profile_name=self._profile_name,
                    )
                for line in response.iter_lines():
                    elapsed = self._clock() - started_at
                    if event_latency is None and elapsed > event_timeout_seconds:
                        raise LlmError(
                            "OpenRouter 事件行超时",
                            latency_seconds=elapsed,
                        )
                    if elapsed > full_timeout_seconds:
                        raise LlmError(
                            "OpenRouter 场面描述超时",
                            latency_seconds=elapsed,
                            partial_event_text=_partial_event_text(text_parts),
                            event_latency_seconds=event_latency,
                        )
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        break
                    chunk = _parse_stream_chunk(data)
                    chunk_model = chunk.get("model")
                    if isinstance(chunk_model, str) and chunk_model.strip():
                        actual_model = chunk_model
                    chunk_provider = chunk.get("provider")
                    if isinstance(chunk_provider, str) and chunk_provider.strip():
                        actual_provider = chunk_provider
                    usage_value = chunk.get("usage")
                    if isinstance(usage_value, Mapping):
                        usage = _parse_usage(usage_value)
                    content = _stream_content(chunk)
                    if content:
                        text_parts.append(content)
                        if event_latency is None and _has_complete_event_line(
                            text_parts
                        ):
                            event_latency = elapsed
        except LlmError as error:
            if error.status_code == 429:
                self._cooldown.enter(error)
            if event_latency is None or error.event_latency_seconds is not None:
                raise
            raise LlmError(
                str(error),
                status_code=error.status_code,
                latency_seconds=(
                    error.latency_seconds
                    if error.latency_seconds is not None
                    else self._clock() - started_at
                ),
                partial_event_text=_partial_event_text(text_parts),
                event_latency_seconds=event_latency,
                **_llm_error_metadata_kwargs(error),
            ) from error
        except httpx.TimeoutException as error:
            latency = self._clock() - started_at
            label = "事件行" if event_latency is None else "场面描述"
            raise LlmError(
                f"OpenRouter {label}超时：{error}",
                latency_seconds=latency,
                partial_event_text=(
                    _partial_event_text(text_parts)
                    if event_latency is not None
                    else None
                ),
                event_latency_seconds=event_latency,
            ) from error
        except httpx.RequestError as error:
            latency = self._clock() - started_at
            raise LlmError(
                f"OpenRouter 网络请求失败：{error}", latency_seconds=latency
            ) from error

        latency = self._clock() - started_at
        self._cooldown.record_success()
        if event_latency is None:
            raise LlmError(
                "OpenRouter 返回中缺少完整事件行",
                latency_seconds=latency,
            )
        try:
            audit_text, event_text, scene_text = parse_analysis_text(
                "".join(text_parts)
            )
        except LlmError as error:
            raise LlmError(
                str(error),
                latency_seconds=latency,
                partial_event_text=_partial_event_text(text_parts),
                event_latency_seconds=event_latency,
            ) from error
        if actual_model is None:
            raise LlmError(
                "OpenRouter 流式响应缺少实际型号 ID",
                latency_seconds=latency,
            )
        return LlmAnalysisResult(
            audit_text=audit_text,
            event_text=event_text,
            scene_text=scene_text,
            usage=usage,
            event_latency_seconds=event_latency,
            latency_seconds=latency,
            model=actual_model,
            provider=actual_provider,
        )

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()

    def dispatch_stats(self) -> LlmDispatchStats:
        """Return this profile's current cooldown accounting."""
        return self._cooldown.snapshot()


def fetch_model_endpoints(
    *,
    profile_name: str,
    base_url: str | None,
    api_key_env: str,
    timeout_seconds: float,
    model: str,
) -> tuple[LlmModelEndpoint, ...] | None:
    """Fetch one profile's live catalog without exposing its concrete client to callers."""
    client = OpenRouterClient.from_profile(
        profile_name=profile_name,
        base_url=base_url,
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
    )
    try:
        return client.list_model_endpoints(model)
    finally:
        client.close()


def _parse_result(payload: object, *, latency_seconds: float) -> LlmResult:
    if not isinstance(payload, Mapping):
        raise TypeError("响应体不是 JSON 对象")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("响应缺少 choices")
    first_choice = choices[0]
    if not isinstance(first_choice, Mapping):
        raise TypeError("choices[0] 不是对象")
    message = first_choice.get("message")
    if not isinstance(message, Mapping):
        raise TypeError("响应缺少 message")
    text_value = message.get("content")
    if text_value is None:
        text = ""
    elif isinstance(text_value, str):
        text = text_value.strip()
    else:
        raise TypeError("响应文本不是字符串或 null")
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("响应缺少实际型号 ID")
    provider_value = payload.get("provider")
    provider = provider_value if isinstance(provider_value, str) else None
    usage_value = payload.get("usage")
    usage = usage_value if isinstance(usage_value, Mapping) else {}
    finish_reason_value = first_choice.get("finish_reason")
    finish_reason = (
        finish_reason_value if isinstance(finish_reason_value, str) else None
    )
    return LlmResult(
        text=text,
        usage=_parse_usage(usage),
        latency_seconds=latency_seconds,
        model=model,
        provider=provider,
        finish_reason=finish_reason,
    )


def parse_analysis_text(text: str) -> tuple[str, str, str]:
    """Parse the exact three-line self-audit protocol used by the benchmark."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if len(lines) != 3:
        raise LlmError("模型输出必须恰好包含核对、事件与场面三行")
    prefixes = ("核对：", "事件：", "场面：")
    if any(not line.startswith(prefix) for line, prefix in zip(lines, prefixes)):
        raise LlmError("模型输出未遵守核对/事件/场面前缀协议")
    audit_text = lines[0].removeprefix("核对：").strip()
    event_text = lines[1].removeprefix("事件：").strip()
    scene_text = lines[2].removeprefix("场面：").strip()
    if not audit_text or not event_text or not scene_text:
        raise LlmError("模型输出的核对、事件或场面为空")
    return audit_text, event_text, scene_text


def _has_complete_event_line(text_parts: list[str]) -> bool:
    return any(
        line.strip().startswith("事件：") and line.endswith(("\n", "\r"))
        for line in "".join(text_parts).splitlines(keepends=True)
    )


def _partial_event_text(text_parts: list[str]) -> str | None:
    text = "".join(text_parts)
    event_line = next(
        (line.strip() for line in text.splitlines() if line.strip().startswith("事件：")),
        None,
    )
    if event_line is None:
        return None
    event_text = event_line.removeprefix("事件：").strip()
    return event_text or None


def _parse_stream_chunk(data: str) -> Mapping[str, object]:
    try:
        payload: object = json.loads(data)
    except ValueError as error:
        raise LlmError(f"OpenRouter 返回了无效 SSE JSON：{error}") from error
    if not isinstance(payload, Mapping):
        raise LlmError("OpenRouter SSE 数据不是 JSON 对象")
    error_value = payload.get("error")
    if error_value is not None:
        error_mapping = error_value if isinstance(error_value, Mapping) else None
        detail = (
            error_mapping.get("message")
            if error_mapping is not None
            and isinstance(error_mapping.get("message"), str)
            else str(error_value)
        )
        error_type, provider_code, provider = _payload_error_fields(payload)
        gateway_code = error_mapping.get("code") if error_mapping is not None else None
        if isinstance(gateway_code, int) and not isinstance(gateway_code, bool):
            status_code = gateway_code if 400 <= gateway_code <= 599 else None
        elif isinstance(gateway_code, str) and gateway_code.isdecimal():
            parsed_status = int(gateway_code)
            status_code = parsed_status if 400 <= parsed_status <= 599 else None
        else:
            status_code = None
        request_id = _first_nonempty_string(
            payload.get("id"),
            error_mapping.get("request_id") if error_mapping is not None else None,
        )
        raise LlmError(
            f"OpenRouter 流式请求失败：{detail}",
            status_code=status_code,
            error_type=error_type,
            provider_code=provider_code,
            provider=provider,
            request_id=request_id,
            request_id_header="SSE payload id" if request_id is not None else None,
        )
    return payload


def _stream_content(chunk: Mapping[str, object]) -> str:
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, Mapping):
        return ""
    delta = first.get("delta")
    if not isinstance(delta, Mapping):
        return ""
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def _stream_finish_reason(chunk: Mapping[str, object]) -> str | None:
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    value = first.get("finish_reason")
    return value if isinstance(value, str) else None


def _streaming_is_unsupported(response: httpx.Response, message: str) -> bool:
    if response.status_code not in {400, 404, 405, 422, 501}:
        return False
    normalized = message.casefold()
    return "stream" in normalized and any(
        marker in normalized
        for marker in ("unsupported", "not support", "does not support", "不支持")
    )


def _parse_usage(usage: Mapping[str, object]) -> LlmUsage:
    details_value = usage.get("completion_tokens_details")
    details = details_value if isinstance(details_value, Mapping) else {}
    reasoning_tokens = _optional_int(details.get("reasoning_tokens"))
    if reasoning_tokens is None:
        reasoning_tokens = _optional_int(usage.get("reasoning_tokens"))
    return LlmUsage(
        prompt_tokens=_optional_int(usage.get("prompt_tokens")),
        completion_tokens=_optional_int(usage.get("completion_tokens")),
        cost_usd=_optional_float(usage.get("cost")),
        reasoning_tokens=reasoning_tokens,
    )


def _response_error_message(
    response: httpx.Response,
    *,
    service_label: str = "OpenRouter",
) -> str:
    detail: str | None = None
    try:
        payload: Any = response.json()
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, Mapping) and isinstance(error.get("message"), str):
                detail = error["message"]
            elif isinstance(error, str):
                detail = error
    except ValueError:
        detail = None
    suffix = f"：{detail}" if detail else ""
    return f"{service_label}请求失败（HTTP {response.status_code}）{suffix}"


def _response_llm_error(
    response: httpx.Response,
    *,
    service_label: str,
    latency_seconds: float | None,
    fallback_provider: str | None,
    profile_name: str,
    error_class: type[LlmError] = LlmError,
) -> LlmError:
    payload: Mapping[str, object] = {}
    try:
        value: object = response.json()
        if isinstance(value, Mapping):
            payload = value
    except ValueError:
        pass
    error_type, provider_code, payload_provider = _payload_error_fields(payload)
    retry_after = response.headers.get("retry-after")
    request_id, request_id_header = _response_request_id(response, payload)
    return error_class(
        _response_error_message(response, service_label=service_label),
        status_code=response.status_code,
        latency_seconds=latency_seconds,
        provider=payload_provider or fallback_provider,
        profile_name=profile_name,
        error_type=error_type,
        provider_code=provider_code,
        retry_after=retry_after,
        retry_after_seconds=_parse_retry_after(retry_after),
        request_id=request_id,
        request_id_header=request_id_header,
    )


def _payload_error_fields(
    payload: Mapping[str, object],
) -> tuple[str | None, str | int | None, str | None]:
    error_value = payload.get("error")
    if not isinstance(error_value, Mapping):
        return None, None, None
    metadata_value = error_value.get("metadata")
    metadata = metadata_value if isinstance(metadata_value, Mapping) else {}
    nested = _nested_provider_error(metadata)
    error_type = _first_nonempty_string(
        nested.get("type") if nested is not None else None,
        nested.get("error_type") if nested is not None else None,
        error_value.get("type"),
        error_value.get("error_type"),
    )
    provider_code = _first_error_code(
        nested.get("code") if nested is not None else None,
        nested.get("error_code") if nested is not None else None,
        error_value.get("code"),
        error_value.get("error_code"),
    )
    provider = _first_nonempty_string(
        metadata.get("provider_name"),
        metadata.get("provider"),
        error_value.get("provider"),
    )
    return error_type, provider_code, provider


def _nested_provider_error(metadata: Mapping[str, object]) -> Mapping[str, object] | None:
    for key in ("provider_error", "raw_error", "raw", "error"):
        value = metadata.get(key)
        if isinstance(value, str):
            try:
                parsed: object = json.loads(value)
            except ValueError:
                continue
            value = parsed
        if not isinstance(value, Mapping):
            continue
        nested = value.get("error")
        if isinstance(nested, Mapping):
            return nested
        return value
    return None


def _response_request_id(
    response: httpx.Response,
    payload: Mapping[str, object],
) -> tuple[str | None, str | None]:
    for header in (
        "x-request-id",
        "x-openrouter-request-id",
        "x-generation-id",
        "request-id",
    ):
        value = response.headers.get(header)
        if value is not None and value.strip():
            return value.strip(), header
    error = payload.get("error")
    error_mapping = error if isinstance(error, Mapping) else {}
    request_id = _first_nonempty_string(
        error_mapping.get("request_id"),
        payload.get("request_id"),
        payload.get("id"),
    )
    return request_id, "response JSON" if request_id is not None else None


def _parse_retry_after(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    try:
        seconds = float(normalized)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(normalized)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (
            retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)
        ).total_seconds()
    return max(0.0, seconds)


def _first_nonempty_string(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_error_code(*values: object) -> str | int | None:
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _llm_error_metadata_kwargs(error: LlmError) -> dict[str, object]:
    return {
        "provider": error.provider,
        "profile_name": error.profile_name,
        "error_type": error.error_type,
        "provider_code": error.provider_code,
        "retry_after": error.retry_after,
        "retry_after_seconds": error.retry_after_seconds,
        "request_id": error.request_id,
        "request_id_header": error.request_id_header,
        "occurred_at": error.occurred_at,
        "cooldown_drop": error.cooldown_drop,
        "cooldown_remaining_seconds": error.cooldown_remaining_seconds,
    }


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _apply_provider_and_reasoning(
    request_body: dict[str, object],
    *,
    provider: str | None,
    seed: int | None,
    reasoning_effort: str | None,
    reasoning_enabled: bool | None,
) -> None:
    if provider is not None:
        # OpenRouter 2026-08-24 官方路由字段：only 限定唯一上游，
        # allow_fallbacks=false 禁止该上游失败后切到别家。
        request_body["provider"] = {
            "only": [provider],
            "allow_fallbacks": False,
        }
    if seed is not None:
        request_body["seed"] = seed
    if reasoning_effort is not None and reasoning_enabled is not None:
        raise ValueError("reasoning effort and enabled flag are mutually exclusive")
    if reasoning_effort is not None:
        if reasoning_effort not in {"none", "minimal", "low", "medium", "high"}:
            raise ValueError("unsupported reasoning effort")
        request_body["reasoning"] = {"effort": reasoning_effort}
    elif reasoning_enabled is not None:
        request_body["reasoning"] = {"enabled": reasoning_enabled}


def _image_messages(
    *,
    system_prompt: str,
    user_prompt: str,
    images: Sequence[LlmImage],
    max_image_edge: int | None,
) -> list[dict[str, object]]:
    if not images:
        raise ValueError("at least one image is required")
    if max_image_edge is not None and max_image_edge <= 0:
        raise ValueError("maximum image edge must be positive")
    user_content: list[dict[str, object]] = [{"type": "text", "text": user_prompt}]
    for attachment in images:
        if not attachment.label.strip():
            raise ValueError("image label must not be blank")
        if attachment.max_edge is not None and attachment.max_edge <= 0:
            raise ValueError("image attachment maximum edge must be positive")
        if attachment.target_width is not None and attachment.target_width <= 0:
            raise ValueError("image attachment target width must be positive")
        effective_max_edge = _smallest_limit(max_image_edge, attachment.max_edge)
        user_content.append({"type": "text", "text": attachment.label})
        user_content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": _image_data_url(
                        attachment,
                        max_image_edge=effective_max_edge,
                    )
                },
            }
        )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def _image_data_url(
    image: LlmImage,
    *,
    max_image_edge: int | None,
) -> str:
    """Load, optionally resize, and re-encode one image without its metadata."""
    payload, _ = _prepare_image_upload(
        image.path,
        max_image_edge=max_image_edge,
        target_width=image.target_width,
        encoding=image.encoding,
        jpeg_quality=image.jpeg_quality,
    )
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:image/{image.encoding};base64,{encoded}"


def probe_llm_profile(
    configuration: object,
    profile_name: str,
    *,
    image_path: Path,
    output: Callable[[str], None] = print,
    injected_client: OpenRouterClient | None = None,
) -> LlmProbeResult:
    """Probe one configured profile without exposing its credential value."""
    from pet.core.config import LlmConfig, resolve_llm_profile

    if not isinstance(configuration, LlmConfig):
        raise TypeError("configuration must be LlmConfig")
    try:
        profile = resolve_llm_profile(configuration, profile_name)
    except ValueError as error:
        output(f"档位检查：失败（{error}）")
        return _probe_failure(profile_name, "", False, str(error))

    environment_is_set = bool(os.environ.get(profile.api_key_env, "").strip())
    output(
        f"a. 环境变量 {profile.api_key_env}："
        f"{'已设置' if environment_is_set else '未设置'}"
    )
    if not environment_is_set:
        error = (
            f"档位 {profile_name} 缺少环境变量 {profile.api_key_env}；"
            "未尝试连接，也未回退"
        )
        output(f"停止：{error}")
        return _probe_failure(
            profile_name, profile.api_key_env, False, error
        )
    if not profile.model.strip():
        error = f"档位 {profile_name} 没有目标模型 ID"
        output(f"停止：{error}")
        return _probe_failure(
            profile_name, profile.api_key_env, True, error
        )

    owns_client = injected_client is None
    client = injected_client or OpenRouterClient.from_profile(
        profile_name=profile_name,
        base_url=profile.base_url,
        api_key_env=profile.api_key_env,
        timeout_seconds=profile.timeout_seconds,
    )
    model_list_status = "未执行"
    attempts: list[ReasoningProbeAttempt] = []
    try:
        try:
            model_ids = client.list_model_ids()
        except LlmError as error:
            output(f"b. 模型列表接口：失败（{error}）")
            return _probe_failure(
                profile_name,
                profile.api_key_env,
                True,
                str(error),
                model_list_status="失败",
            )
        if model_ids is None:
            model_list_status = "跳过（端点不支持）"
            output(f"b. 模型列表接口：{model_list_status}")
        else:
            model_list_status = "可达"
            output("b. 模型列表接口：可达")
            if profile.model not in model_ids:
                error = f"模型列表中没有档位 {profile_name} 的目标模型 ID"
                output(f"停止：{error}")
                return _probe_failure(
                    profile_name,
                    profile.api_key_env,
                    True,
                    error,
                    model_list_status=model_list_status,
                )

        try:
            omitted_result = client.complete(
                model=profile.model,
                provider=profile.provider or None,
                system_prompt="只回答：OK",
                user_prompt="连通性检查。",
                max_tokens=8,
                temperature=0.0,
            )
        except (LlmError, ValueError) as error:
            output(f"c. 极小纯文本调用：失败（{error}）")
            return _probe_failure(
                profile_name,
                profile.api_key_env,
                True,
                str(error),
                model_list_status=model_list_status,
            )
        output("c. 极小纯文本调用：通过")
        attempts.append(
            ReasoningProbeAttempt(
                "omitted",
                True,
                omitted_result.usage.reasoning_tokens,
                None,
            )
        )

        for mode in ("effort_none", "enabled_false"):
            typed_mode = cast(ReasoningParameterMode, mode)
            arguments = _reasoning_arguments(typed_mode)
            try:
                result = client.complete(
                    model=profile.model,
                    provider=profile.provider or None,
                    system_prompt="只回答：OK",
                    user_prompt="推理参数兼容性检查。",
                    max_tokens=8,
                    temperature=0.0,
                    **arguments,
                )
            except (LlmError, ValueError) as error:
                attempts.append(
                    ReasoningProbeAttempt(typed_mode, False, None, str(error))
                )
                output(f"推理参数 {typed_mode}：拒绝（{error}）")
            else:
                attempts.append(
                    ReasoningProbeAttempt(
                        typed_mode,
                        True,
                        result.usage.reasoning_tokens,
                        None,
                    )
                )
                tokens = result.usage.reasoning_tokens
                output(
                    f"推理参数 {typed_mode}：接受；推理 token="
                    f"{tokens if tokens is not None else '未返回'}"
                )
        selected_mode = _select_reasoning_mode(attempts)
        output(f"推理参数实测选择：{selected_mode}")
        reasoning_arguments = _reasoning_arguments(selected_mode)

        try:
            client.complete_with_images(
                model=profile.model,
                provider=profile.provider or None,
                system_prompt="只描述画面中可见的颜色或形状。",
                user_prompt="图像连通性检查。",
                images=(LlmImage(image_path, "合成测试图", max_edge=64),),
                max_image_edge=64,
                max_tokens=80,
                temperature=0.0,
                **reasoning_arguments,
            )
        except (LlmError, ValueError, OSError) as error:
            output(f"d. 图像调用：失败（{error}）")
            return _probe_failure(
                profile_name,
                profile.api_key_env,
                True,
                str(error),
                model_list_status=model_list_status,
                attempts=tuple(attempts),
                selected_mode=selected_mode,
                text_available=True,
                model_available=True,
            )
        output("d. 图像调用：通过")

        try:
            streamed = client.complete_with_images_stream(
                model=profile.model,
                provider=profile.provider or None,
                system_prompt="只描述画面中可见的颜色或形状。",
                user_prompt="流式图像连通性检查。",
                images=(LlmImage(image_path, "合成测试图", max_edge=64),),
                max_image_edge=64,
                max_tokens=80,
                temperature=0.0,
                **reasoning_arguments,
            )
        except (LlmError, ValueError, OSError) as error:
            output(f"e. 流式调用：失败（{error}）")
            return _probe_failure(
                profile_name,
                profile.api_key_env,
                True,
                str(error),
                model_list_status=model_list_status,
                attempts=tuple(attempts),
                selected_mode=selected_mode,
                text_available=True,
                model_available=True,
                image_available=True,
            )
        ttft_ms = (
            streamed.ttft_seconds * 1000
            if streamed.ttft_seconds is not None
            else None
        )
        latency_ms = streamed.latency_seconds * 1000
        output(
            "e. 流式调用：通过；TTFT="
            f"{ttft_ms:.3f} ms；总时延={latency_ms:.3f} ms"
            if ttft_ms is not None
            else f"e. 流式调用：失败（响应没有可测 TTFT）；总时延={latency_ms:.3f} ms"
        )
        if ttft_ms is None:
            return _probe_failure(
                profile_name,
                profile.api_key_env,
                True,
                "流式响应没有可测 TTFT",
                model_list_status=model_list_status,
                attempts=tuple(attempts),
                selected_mode=selected_mode,
                text_available=True,
                model_available=True,
                image_available=True,
            )
        return LlmProbeResult(
            profile_name=profile_name,
            environment_variable=profile.api_key_env,
            environment_is_set=True,
            model_list_status=model_list_status,
            model_available=True,
            text_available=True,
            image_available=True,
            streaming_available=True,
            ttft_ms=ttft_ms,
            latency_ms=latency_ms,
            reasoning_attempts=tuple(attempts),
            selected_reasoning_mode=selected_mode,
            error=None,
        )
    finally:
        if owns_client:
            client.close()


def _probe_failure(
    profile_name: str,
    environment_variable: str,
    environment_is_set: bool,
    error: str,
    *,
    model_list_status: str = "未执行",
    attempts: tuple[ReasoningProbeAttempt, ...] = (),
    selected_mode: ReasoningParameterMode | None = None,
    model_available: bool = False,
    text_available: bool = False,
    image_available: bool = False,
) -> LlmProbeResult:
    return LlmProbeResult(
        profile_name=profile_name,
        environment_variable=environment_variable,
        environment_is_set=environment_is_set,
        model_list_status=model_list_status,
        model_available=model_available,
        text_available=text_available,
        image_available=image_available,
        streaming_available=False,
        ttft_ms=None,
        latency_ms=None,
        reasoning_attempts=attempts,
        selected_reasoning_mode=selected_mode,
        error=error,
    )


def _reasoning_arguments(mode: ReasoningParameterMode) -> dict[str, object]:
    if mode == "effort_none":
        return {"reasoning_effort": "none"}
    if mode == "enabled_false":
        return {"reasoning_enabled": False}
    return {}


def _select_reasoning_mode(
    attempts: Sequence[ReasoningProbeAttempt],
) -> ReasoningParameterMode:
    accepted = [attempt for attempt in attempts if attempt.accepted]
    if not accepted:
        raise LlmError("三种推理参数写法均被拒绝")
    # When usage metadata cannot prove how many hidden reasoning tokens were used,
    # prefer an accepted explicit disable spelling over silently omitting the field.
    # This also makes the probe exercise the same setting used by production calls.
    order = {"enabled_false": 0, "effort_none": 1, "omitted": 2}
    selected = min(
        accepted,
        key=lambda attempt: (
            attempt.reasoning_tokens is None,
            attempt.reasoning_tokens if attempt.reasoning_tokens is not None else 0,
            order[attempt.mode],
        ),
    )
    return selected.mode


def build_probe_parser() -> argparse.ArgumentParser:
    """Build the standalone profile connectivity probe CLI."""
    from pet.core.config import DEFAULT_CONFIG_PATH, LOCAL_CONFIG_PATH

    parser = argparse.ArgumentParser(description="OpenAI 兼容模型档位连通性探针")
    parser.add_argument("--probe", action="store_true", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--local-config", type=Path, default=LOCAL_CONFIG_PATH)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path(__file__).resolve().parents[3]
        / "tests"
        / "fixtures"
        / "vision-exam-frame-a.ppm",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run only the explicit foreground connectivity probe."""
    from pet.core.config import load_config

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    arguments = build_probe_parser().parse_args(argv)
    configuration = load_config(
        arguments.config,
        arguments.local_config,
        strict=True,
    )
    result = probe_llm_profile(
        configuration.llm,
        arguments.profile,
        image_path=arguments.fixture,
    )
    print(f"探针结论：{'全部通过' if result.passed else '未通过'}")
    return 0 if result.passed else 2


def image_upload_metadata(
    image: LlmImage,
    *,
    max_image_edge: int | None,
) -> LlmImageUploadMetadata:
    """Inspect the exact locally encoded payload without sending it anywhere."""
    if max_image_edge is not None and max_image_edge <= 0:
        raise ValueError("maximum image edge must be positive")
    if image.max_edge is not None and image.max_edge <= 0:
        raise ValueError("image attachment maximum edge must be positive")
    if image.target_width is not None and image.target_width <= 0:
        raise ValueError("image attachment target width must be positive")
    payload, size = _prepare_image_upload(
        image.path,
        max_image_edge=_smallest_limit(max_image_edge, image.max_edge),
        target_width=image.target_width,
        encoding=image.encoding,
        jpeg_quality=image.jpeg_quality,
    )
    return LlmImageUploadMetadata(size[0], size[1], len(payload))


def _prepare_image_upload(
    path: Path | Image.Image,
    *,
    max_image_edge: int | None,
    target_width: int | None,
    encoding: Literal["png", "jpeg"] = "png",
    jpeg_quality: int = 85,
) -> tuple[bytes, tuple[int, int]]:
    if encoding == "jpeg" and not 1 <= jpeg_quality <= 95:
        raise ValueError("JPEG quality must be between 1 and 95")
    if isinstance(path, Image.Image):
        image = path.convert("RGBA" if path.has_transparency_data else "RGB")
    else:
        try:
            with Image.open(path) as source:
                source.load()
                image = source.convert("RGBA" if source.has_transparency_data else "RGB")
        except (OSError, ValueError) as error:
            raise LlmError(f"无法读取待上传图像 {path}：{error}") from error

    if target_width is not None and image.width != target_width:
        target_height = max(1, round(image.height * target_width / image.width))
        image = image.resize(
            (target_width, target_height),
            resample=Image.Resampling.LANCZOS,
        )
    if max_image_edge is not None and max(image.size) > max_image_edge:
        image.thumbnail(
            (max_image_edge, max_image_edge),
            resample=Image.Resampling.LANCZOS,
        )
    output = BytesIO()
    if encoding == "jpeg":
        image = image.convert("RGB")
        image.save(output, format="JPEG", quality=jpeg_quality, optimize=True)
    else:
        image.save(output, format="PNG", optimize=True)
    return output.getvalue(), image.size


def _smallest_limit(first: int | None, second: int | None) -> int | None:
    limits = tuple(value for value in (first, second) if value is not None)
    return min(limits) if limits else None


if __name__ == "__main__":
    raise SystemExit(main())
