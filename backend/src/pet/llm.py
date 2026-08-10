"""Provider-neutral language-model result types and an OpenRouter client."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
import time
from typing import Any, Protocol

import httpx

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass(frozen=True, slots=True)
class LlmUsage:
    """One call's token accounting as reported by the upstream."""

    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd: float | None


@dataclass(frozen=True, slots=True)
class LlmResult:
    """The text, accounting, and wall-clock cost of one completed call."""

    text: str
    usage: LlmUsage
    latency_seconds: float
    model: str
    provider: str | None


class LlmError(Exception):
    """One failed call, carrying enough context to appear in a report."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        latency_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.latency_seconds = latency_seconds


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
    ) -> LlmResult:
        """Return one completed response without retrying."""
        ...


class OpenRouterClient:
    """Minimal non-streaming client for OpenRouter chat completions."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = OPENROUTER_BASE_URL,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise LlmError("OpenRouter API 密钥为空")
        self._client = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    @classmethod
    def from_env(
        cls,
        *,
        base_url: str = OPENROUTER_BASE_URL,
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
    ) -> LlmResult:
        """Send one non-streaming chat completion and never retry failures."""
        started_at = time.perf_counter()
        request_body: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if provider is not None:
            request_body["provider"] = {
                "only": [provider],
                "allow_fallbacks": False,
            }

        try:
            response = self._client.post(
                "chat/completions",
                json=request_body,
            )
        except httpx.TimeoutException as error:
            latency = time.perf_counter() - started_at
            raise LlmError(
                f"OpenRouter 请求超时：{error}", latency_seconds=latency
            ) from error
        except httpx.RequestError as error:
            latency = time.perf_counter() - started_at
            raise LlmError(
                f"OpenRouter 网络请求失败：{error}", latency_seconds=latency
            ) from error

        latency = time.perf_counter() - started_at
        if not response.is_success:
            raise LlmError(
                _response_error_message(response),
                status_code=response.status_code,
                latency_seconds=latency,
            )

        try:
            payload: object = response.json()
            result = _parse_result(payload, latency_seconds=latency)
        except (TypeError, ValueError, KeyError, IndexError) as error:
            raise LlmError(
                f"OpenRouter 返回了无法解析的成功响应：{error}",
                status_code=response.status_code,
                latency_seconds=latency,
            ) from error
        return result

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()


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
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("响应文本为空")
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("响应缺少实际型号 ID")
    provider_value = payload.get("provider")
    provider = provider_value if isinstance(provider_value, str) else None
    usage_value = payload.get("usage")
    usage = usage_value if isinstance(usage_value, Mapping) else {}
    return LlmResult(
        text=text.strip(),
        usage=LlmUsage(
            prompt_tokens=_optional_int(usage.get("prompt_tokens")),
            completion_tokens=_optional_int(usage.get("completion_tokens")),
            cost_usd=_optional_float(usage.get("cost")),
        ),
        latency_seconds=latency_seconds,
        model=model,
        provider=provider,
    )


def _response_error_message(response: httpx.Response) -> str:
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
    return f"OpenRouter 请求失败（HTTP {response.status_code}）{suffix}"


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None
