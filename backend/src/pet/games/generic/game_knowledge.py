"""One-shot, web-backed shelf-one game knowledge for each game session."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import logging
import re
import time
from typing import Protocol
import uuid

from pydantic import ValidationError

from pet.core.config import LlmConfig
from pet.core.gamecard import (
    CANONICAL_PC_INPUT_PATTERN,
    GameKnowledgeContent,
    GameKnowledgeOutcome,
)
from pet.core.llm import (
    LlmCooldownError,
    LlmDispatchStats,
    LlmError,
    LlmResult,
)
from pet.core.prompt import PROMPTS_DIRECTORY

logger = logging.getLogger(__name__)

GAME_KNOWLEDGE_PROMPT_PATH = (
    PROMPTS_DIRECTORY / "generic" / "game-knowledge.md"
)
GAME_KNOWLEDGE_MODE = "web"
WEB_SEARCH_PARAMETERS: dict[str, object] = {
    "engine": "exa",
    "max_results": 5,
    "max_total_results": 5,
}
PROVIDER_OPTIONS: dict[str, object] = {
    "sort": "throughput",
    "require_parameters": True,
}
RESPONSE_PLUGINS: tuple[dict[str, object], ...] = (
    {"id": "response-healing"},
)
RESPONSE_FORMAT: dict[str, object] = {
    "type": "json_schema",
    "json_schema": {
        "name": "game_knowledge_context",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "genre",
                "perspective",
                "game_overview",
                "gameplay",
                "background",
                "default_pc_keybinds",
            ],
            "properties": {
                "genre": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "items": {"type": "string", "minLength": 1},
                },
                "perspective": {"type": "string", "minLength": 1},
                "game_overview": {"type": "string", "minLength": 1},
                "gameplay": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "player_goal",
                        "core_loop",
                        "major_systems",
                        "modes_and_structure",
                    ],
                    "properties": {
                        "player_goal": {"type": "string", "minLength": 1},
                        "core_loop": {"type": "string", "minLength": 1},
                        "major_systems": {
                            "type": "array",
                            "minItems": 4,
                            "maxItems": 10,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["name", "description"],
                                "properties": {
                                    "name": {"type": "string", "minLength": 1},
                                    "description": {
                                        "type": "string",
                                        "minLength": 1,
                                    },
                                },
                            },
                        },
                        "modes_and_structure": {
                            "type": "string",
                            "minLength": 1,
                        },
                    },
                },
                "background": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "setting_and_premise",
                        "release_and_service_status",
                    ],
                    "properties": {
                        "setting_and_premise": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "release_and_service_status": {
                            "type": "string",
                            "minLength": 1,
                        },
                    },
                },
                "default_pc_keybinds": {
                    "type": "object",
                    "maxProperties": 40,
                    "propertyNames": {"type": "string", "minLength": 1},
                    "additionalProperties": {
                        "type": "string",
                        "pattern": CANONICAL_PC_INPUT_PATTERN,
                    },
                },
            },
        },
    },
}

_PC_INPUT_ALIASES = {
    "`": "Backquote",
    "-": "Minus",
    "=": "Equals",
    "[": "LeftBracket",
    "]": "RightBracket",
    "\\": "Backslash",
    ";": "Semicolon",
    "'": "Apostrophe",
    ",": "Comma",
    ".": "Period",
    "/": "Slash",
}


class GameKnowledgeClientProtocol(Protocol):
    async def complete_with_web_search(
        self,
        *,
        model: str,
        provider: str | None,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str,
        web_search_parameters: Mapping[str, object],
        provider_options: Mapping[str, object],
        response_format: Mapping[str, object],
        plugins: Sequence[Mapping[str, object]],
    ) -> LlmResult: ...

    def dispatch_stats(self) -> LlmDispatchStats: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class GameKnowledgeParseResult:
    content: GameKnowledgeContent | None
    error: str | None
    normalization_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GameKnowledgeCallResult:
    request_id: str
    outcome: GameKnowledgeOutcome
    model: str
    actual_model: str | None
    provider: str | None
    latency_ms: float
    cost_usd: float
    prompt_tokens: int | None
    completion_tokens: int | None
    content: GameKnowledgeContent | None
    failure_reason: str | None
    normalization_actions: tuple[str, ...]
    error_metadata: dict[str, object] | None = None


class GameKnowledgeReader:
    """Dispatch exactly one non-streaming web lookup with a wall-clock cap."""

    def __init__(
        self,
        client: GameKnowledgeClientProtocol,
        configuration: LlmConfig,
        *,
        wall_timeout_seconds: float,
        request_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if not configuration.enabled or not configuration.model.strip():
            raise ValueError("game knowledge profile must be enabled and name a model")
        if wall_timeout_seconds <= 0:
            raise ValueError("game knowledge wall timeout must be positive")
        self._client = client
        self._configuration = configuration
        self._wall_timeout_seconds = wall_timeout_seconds
        self._request_id_factory = request_id_factory or (
            lambda: f"gk-{uuid.uuid4()}"
        )
        self._clock = clock

    async def read(self, game_name: str) -> GameKnowledgeCallResult:
        request_id = self._request_id_factory()
        if not request_id.strip():
            raise ValueError("game knowledge request id must not be blank")
        started = self._clock()
        try:
            result = await asyncio.wait_for(
                self._client.complete_with_web_search(
                    model=self._configuration.model,
                    provider=self._configuration.provider or None,
                    system_prompt=GAME_KNOWLEDGE_PROMPT_PATH.read_text(
                        encoding="utf-8"
                    ).strip(),
                    user_prompt=f"游戏名称：{game_name}",
                    max_tokens=self._configuration.max_tokens,
                    temperature=self._configuration.temperature,
                    reasoning_effort="minimal",
                    web_search_parameters=WEB_SEARCH_PARAMETERS,
                    provider_options=PROVIDER_OPTIONS,
                    response_format=RESPONSE_FORMAT,
                    plugins=RESPONSE_PLUGINS,
                ),
                timeout=self._wall_timeout_seconds,
            )
        except TimeoutError:
            return self._failure(
                request_id,
                "timeout",
                "游戏知识调用超过总墙钟截止；请求已取消，上游是否计费未知",
                started,
                error_metadata={
                    "error_type": "wall_timeout",
                    "cost_accounting": "unknown_after_cancellation",
                },
            )
        except LlmCooldownError as error:
            return self._failure(
                request_id,
                "cooldown_drop",
                error.diagnostic(),
                started,
                error_metadata=error.metadata(),
            )
        except LlmError as error:
            outcome: GameKnowledgeOutcome = (
                "timeout" if error.error_type == "timeout" else "failed"
            )
            return self._failure(
                request_id,
                outcome,
                error.diagnostic(),
                started,
                latency_seconds=error.latency_seconds,
                error_metadata=error.metadata(),
            )
        except (OSError, UnicodeError, ValueError) as error:
            return self._failure(
                request_id,
                "failed",
                f"游戏知识本地准备失败：{error}",
                started,
                error_metadata={"error_type": "local_preparation"},
            )
        except Exception as error:
            # Every started session attempt must leave a failed card attempt and
            # evidence record.  Unexpected local bugs are logged with traceback;
            # they are not allowed to escape as an unobserved background task.
            logger.exception("游戏知识调用出现未预期异常")
            return self._failure(
                request_id,
                "failed",
                f"游戏知识未预期异常：{type(error).__name__}: {error}",
                started,
                error_metadata={"error_type": "unexpected_local"},
            )

        parsed = parse_game_knowledge_response(result.text)
        if result.finish_reason == "length":
            parsed = GameKnowledgeParseResult(
                None,
                "输出因 length 截断，无法形成完整合同 JSON",
                parsed.normalization_actions,
            )
        if parsed.content is None:
            return GameKnowledgeCallResult(
                request_id=request_id,
                outcome="schema_reject",
                model=self._configuration.model,
                actual_model=result.model,
                provider=result.provider,
                latency_ms=max(0.0, result.latency_seconds * 1000.0),
                cost_usd=result.usage.cost_usd or 0.0,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                content=None,
                failure_reason=parsed.error,
                normalization_actions=parsed.normalization_actions,
            )
        return GameKnowledgeCallResult(
            request_id=request_id,
            outcome="ok",
            model=self._configuration.model,
            actual_model=result.model,
            provider=result.provider,
            latency_ms=max(0.0, result.latency_seconds * 1000.0),
            cost_usd=result.usage.cost_usd or 0.0,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            content=parsed.content,
            failure_reason=None,
            normalization_actions=parsed.normalization_actions,
        )

    def close(self) -> None:
        self._client.close()

    def dispatch_stats(self) -> LlmDispatchStats:
        return self._client.dispatch_stats()

    def _failure(
        self,
        request_id: str,
        outcome: GameKnowledgeOutcome,
        reason: str,
        started: float,
        *,
        latency_seconds: float | None = None,
        error_metadata: dict[str, object] | None = None,
    ) -> GameKnowledgeCallResult:
        elapsed = (
            max(0.0, latency_seconds)
            if latency_seconds is not None
            else max(0.0, self._clock() - started)
        )
        return GameKnowledgeCallResult(
            request_id=request_id,
            outcome=outcome,
            model=self._configuration.model,
            actual_model=None,
            provider=self._configuration.provider or None,
            latency_ms=elapsed * 1000.0,
            cost_usd=0.0,
            prompt_tokens=None,
            completion_tokens=None,
            content=None,
            failure_reason=" ".join(reason.split()),
            normalization_actions=(),
            error_metadata=error_metadata,
        )


class _DuplicateJsonKeyError(ValueError):
    pass


class _NonstandardJsonConstantError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(f"JSON 含重复键：{key!r}")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str) -> object:
    raise _NonstandardJsonConstantError(f"JSON 含非标准常量：{value}")


def _strict_json_loads(text: str) -> object:
    return json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonstandard_constant,
    )


def _balanced_json_objects(text: str) -> tuple[str, ...]:
    objects: list[str] = []
    start: int | None = None
    depth = 0
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start : index + 1])
                start = None
    return tuple(objects)


def _remove_trailing_commas(text: str) -> tuple[str, int]:
    output: list[str] = []
    in_string = False
    escaped = False
    removed = 0
    index = 0
    while index < len(text):
        character = text[index]
        if in_string:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            output.append(character)
            index += 1
            continue
        if character == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                removed += 1
                index += 1
                continue
        output.append(character)
        index += 1
    return "".join(output), removed


def _normalize_keybind_aliases(
    value: object,
) -> tuple[object, tuple[str, ...]]:
    if not isinstance(value, dict):
        return value, ()
    keybinds = value.get("default_pc_keybinds")
    if not isinstance(keybinds, dict):
        return value, ()
    normalized_keybinds: dict[object, object] = {}
    actions: list[str] = []
    for action, input_name in keybinds.items():
        normalized = _PC_INPUT_ALIASES.get(input_name, input_name)
        normalized_keybinds[action] = normalized
        if normalized != input_name:
            actions.append(
                f"键位输入 {input_name!r} → {normalized!r}（动作：{action}）"
            )
    if not actions:
        return value, ()
    normalized_value = dict(value)
    normalized_value["default_pc_keybinds"] = normalized_keybinds
    return normalized_value, tuple(actions)


def _parse_candidate(
    text: str,
) -> GameKnowledgeParseResult:
    repaired, comma_count = _remove_trailing_commas(text)
    try:
        value = _strict_json_loads(repaired)
    except json.JSONDecodeError as error:
        return GameKnowledgeParseResult(
            None,
            f"不是合法 JSON：{error.msg}（line {error.lineno}, column {error.colno}）",
            (),
        )
    except (_DuplicateJsonKeyError, _NonstandardJsonConstantError) as error:
        return GameKnowledgeParseResult(None, str(error), ())
    value, alias_actions = _normalize_keybind_aliases(value)
    actions = (
        ((f"移除 {comma_count} 个对象／数组尾随逗号",) if comma_count else ())
        + alias_actions
    )
    try:
        content = GameKnowledgeContent.model_validate(value, strict=True)
        canonical = json.dumps(
            content.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        round_trip = GameKnowledgeContent.model_validate(
            _strict_json_loads(canonical),
            strict=True,
        )
    except (ValidationError, TypeError, ValueError) as error:
        return GameKnowledgeParseResult(
            None,
            f"未通过完整 V3 合同：{' '.join(str(error).split())}",
            actions,
        )
    if round_trip != content:
        return GameKnowledgeParseResult(
            None,
            "规范 JSON 二次解析未通过同一合同",
            actions,
        )
    return GameKnowledgeParseResult(content, None, actions)


def parse_game_knowledge_response(text: str) -> GameKnowledgeParseResult:
    """Apply only the ambiguity-free normalizations accepted by T3a V3."""
    if not text.strip():
        return GameKnowledgeParseResult(None, "空答", ())
    direct = _parse_candidate(text)
    if direct.content is not None:
        return direct
    candidates = _balanced_json_objects(text)
    if len(candidates) == 1 and candidates[0] == text.strip():
        return direct
    valid: list[GameKnowledgeParseResult] = []
    errors: list[str] = []
    for candidate in candidates:
        parsed = _parse_candidate(candidate)
        if parsed.content is not None:
            valid.append(parsed)
        elif parsed.error is not None and parsed.error not in errors:
            errors.append(parsed.error)
    if len(valid) == 1:
        parsed = valid[0]
        return GameKnowledgeParseResult(
            parsed.content,
            None,
            ("剥离 JSON 外文本／代码围栏", *parsed.normalization_actions),
        )
    if len(valid) > 1:
        return GameKnowledgeParseResult(
            None,
            "响应中存在多个符合合同的 JSON 对象，无法确定唯一结果",
            (),
        )
    if errors:
        return GameKnowledgeParseResult(
            None,
            f"找到完整 JSON 对象，但未通过严格合同：{'；'.join(errors[:3])}",
            (),
        )
    return direct
