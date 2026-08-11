"""Provider-neutral language-model result types and an OpenRouter client."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
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
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.latency_seconds = latency_seconds
        self.partial_event_text = partial_event_text
        self.event_latency_seconds = event_latency_seconds


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
    ) -> LlmAnalysisResult:
        """Return the first event line and complete scene without retrying."""
        ...


class OpenRouterClient:
    """Minimal client for non-streaming and streamed OpenRouter completions."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = OPENROUTER_BASE_URL,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if not api_key.strip():
            raise LlmError("OpenRouter API 密钥为空")
        self._client = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )
        self._clock = clock

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
    ) -> LlmAnalysisResult:
        """Stream one strict event/scene response and never retry failures."""
        if event_timeout_seconds <= 0:
            raise ValueError("event timeout must be positive")
        if full_timeout_seconds < event_timeout_seconds:
            raise ValueError("full timeout must be at least the event timeout")

        started_at = self._clock()
        request_body: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "reasoning": {"effort": "none"},
        }
        if provider is not None:
            request_body["provider"] = {
                "only": [provider],
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
                    raise LlmError(
                        _response_error_message(response),
                        status_code=response.status_code,
                        latency_seconds=self._clock() - started_at,
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
        usage=_parse_usage(usage),
        latency_seconds=latency_seconds,
        model=model,
        provider=provider,
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
        if isinstance(error_value, Mapping) and isinstance(
            error_value.get("message"), str
        ):
            detail = error_value["message"]
        else:
            detail = str(error_value)
        raise LlmError(f"OpenRouter 流式请求失败：{detail}")
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


def _parse_usage(usage: Mapping[str, object]) -> LlmUsage:
    return LlmUsage(
        prompt_tokens=_optional_int(usage.get("prompt_tokens")),
        completion_tokens=_optional_int(usage.get("completion_tokens")),
        cost_usd=_optional_float(usage.get("cost")),
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
