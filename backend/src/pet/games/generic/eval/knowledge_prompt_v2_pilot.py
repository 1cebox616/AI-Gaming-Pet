"""Run a small DeepSeek V4 pilot for the detailed game-context prompt."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import statistics
from typing import Protocol

from pet.core.llm import LlmDispatchStats, LlmError, LlmResult
from pet.games.generic.eval.knowledge_model_probe import (
    BACKEND_DIRECTORY,
    MODES,
    OPENROUTER_WEB_SEARCH_DOC,
    GameCase,
    ProbeMode,
    ProbeOpenRouterClient,
)


DEFAULT_OUTPUT = (
    BACKEND_DIRECTORY / "eval-reports" / "m5-b-t3a" / "deepseek-v4-pilot"
)
MODEL = "deepseek/deepseek-v4-pro-0813"
MODEL_LABEL = "DeepSeek V4 Pro 0813"
MODEL_URL = "https://openrouter.ai/deepseek/deepseek-v4-pro-0813"
PROVIDER: str | None = None
REASONING_EFFORT: str | None = None
TEMPERATURE = 0.0
# Safety ceiling rather than a requested answer length. The prompt itself does not
# impose a word limit; 2400 truncated the Rainbow Six pilot response.
MAX_TOKENS = 8000
TIMEOUT_SECONDS = 45.0
LATENCY_TARGET_SECONDS = 10.0
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
INITIAL_GAMES = (
    GameCase(
        "overwatch-2",
        "Overwatch 2",
        "守望先锋",
        "英雄射击",
        "长线运营",
        "录像游戏；团队英雄射击与大量英雄专属键位。",
    ),
    GameCase(
        "rainbow-six-siege",
        "Tom Clancy's Rainbow Six Siege",
        "彩虹六号：围攻",
        "战术射击",
        "长线运营",
        "检验战术射击、破坏系统与姿态键位。",
    ),
    GameCase(
        "genshin-impact",
        "Genshin Impact",
        "原神",
        "开放世界动作 RPG",
        "热门长线运营",
        "检验动作 RPG、队伍切换与快捷菜单键位。",
    ),
    GameCase(
        "kingdom-come-deliverance-2",
        "Kingdom Come: Deliverance II",
        "天国：拯救 2",
        "开放世界角色扮演",
        "2025 新作",
        "检验复杂第一人称 RPG 与上下文动作键位。",
    ),
    GameCase(
        "black-myth-wukong",
        "Black Myth: Wukong",
        "黑神话：悟空",
        "动作角色扮演",
        "热门近年作品",
        "检验第三人称动作游戏与战斗键位。",
    ),
)
FOLLOWUP_GAMES = (
    GameCase(
        "marvel-rivals",
        "Marvel Rivals",
        "漫威争锋",
        "第三人称英雄射击",
        "热门长线运营",
        "检验英雄技能、团队目标与角色差异化键位。",
    ),
    GameCase(
        "monster-hunter-wilds",
        "Monster Hunter Wilds",
        "怪物猎人：荒野",
        "动作角色扮演",
        "2025 新作",
        "检验复杂武器动作、狩猎循环与上下文组合键。",
    ),
    GameCase(
        "sid-meiers-civilization-vii",
        "Sid Meier's Civilization VII",
        "席德·梅尔的文明 VII",
        "回合制策略",
        "2025 新作",
        "检验鼠标主导策略游戏、时代推进与回合结构。",
    ),
    GameCase(
        "ea-sports-fc-26",
        "EA Sports FC 26",
        "EA Sports FC 26",
        "体育模拟",
        "2025 年度作品",
        "检验以手柄为主流但仍需给出 PC 键盘默认绑定的边界。",
    ),
    GameCase(
        "hades-ii",
        "Hades II",
        "哈迪斯 II",
        "动作肉鸽",
        "近年持续更新作品",
        "检验即时战斗、局内循环与永久成长结构。",
    ),
)
HEALING_GAMES = (
    FOLLOWUP_GAMES[0],
    FOLLOWUP_GAMES[3],
    FOLLOWUP_GAMES[4],
)
GAME_SUITES = {
    "initial": INITIAL_GAMES,
    "followup": FOLLOWUP_GAMES,
    "healing": HEALING_GAMES,
}
ACTIVE_GAME_SUITE = "initial"


SYSTEM_PROMPT_V2 = """你是“游戏知识线”的公开资料整理器。你的输出会作为稳定的游戏背景 context，提供给每一个后续视觉模型。准确、完整和可核查优先，不要为了简短而省略决定游戏如何游玩的关键信息。

只回答玩家开始游玩前可从官方页面、商店页、游戏内公开说明或可靠公开资料得知的通用知识。若调用环境提供联网工具，先用它核查当前版本与公开资料；若没有联网工具，则使用自身知识。不得把不确定内容猜成事实，不确定时明确写“不确定”。

内容边界：可以介绍游戏定位、玩法结构、规则系统、公开的世界设定前提与运营状态；不得提供剧情推进、具体任务或关卡解法、角色命运、结局、具体地图内容、隐藏内容或剧透。不要描述 HUD；不要输出社区术语。默认键位只写 PC 默认键盘鼠标，不写主机或控制器键位，不把可由玩家修改的绑定说成唯一操作方式。

只输出一个合法 JSON 对象，不要 Markdown、代码围栏、引用列表或额外说明。顶层字段必须恰好如下：
{
  "genre": ["主要类型", "必要时补充子类型"],
  "perspective": "玩家通常采用的视角；存在多种时说明切换关系",
  "game_overview": "完整介绍游戏定位、玩家扮演的抽象角色、主要目标、单人或多人形态，以及区别于同类游戏的关键特征",
  "gameplay": {
    "player_goal": "玩家在典型游玩过程中追求什么",
    "core_loop": "按时间顺序详细说明反复发生的核心玩法循环",
    "major_systems": [
      {"name": "重要系统名称", "description": "该系统如何影响玩家决策和行动"}
    ],
    "modes_and_structure": "一局、一次远征、一个回合或持续世界如何组织；说明合作、对抗或单人结构"
  },
  "background": {
    "setting_and_premise": "不剧透的世界背景、时代或题材前提，只写理解画面与玩法所需内容",
    "release_and_service_status": "公开的发售、抢先体验、长线运营或重大版本状态；无法确认当前状态时写不确定"
  },
  "default_pc_keybinds": {
    "前进": "W",
    "快捷物品栏1": "1"
  }
}

详细度要求：
- game_overview 应为信息密度高的完整段落，不是一句话宣传语。
- gameplay.core_loop 应覆盖开始、进行、反馈与继续循环；major_systems 写 4 至 10 个真正影响玩法的系统。
- background 只提供理解游戏所需的公开前提，不复述故事。
- default_pc_keybinds 是“动作名称 → 单一规范化 PC 输入”的对象，动作名称使用简体中文且不可重复。每个值只允许一个具体按键、鼠标输入或用 + 连接的组合键。
- 移动方向和快捷栏必须逐项展开，例如“前进":"W"、“后退":"S"、“快捷物品栏1":"1"；禁止写成 WASD、1-0、Q/E、“某键或某键”或任何范围／备选缩写。
- 输入名使用固定英文形式：单字母 A-Z、数字 0-9、F1-F24、Space、Tab、Escape、Enter、Backspace、CapsLock、LeftShift、RightShift、LeftCtrl、RightCtrl、LeftAlt、RightAlt、方向键 ArrowUp/ArrowDown/ArrowLeft/ArrowRight、MouseLeft、MouseRight、MouseMiddle、Mouse1-Mouse5、MouseWheelUp、MouseWheelDown、MouseMove，以及常见标点键名；组合键用 +，例如 LeftCtrl+F。只收录能够从公开资料确认的默认绑定；不确定的条目直接省略，不要猜测。
- 所有字段使用简体中文；每个事实应能单独判定为对、错或不确定。"""

USER_PROMPT_TEMPLATE = "游戏名称：{game_name}"

ANSWER_FIELDS = (
    ("genre", "类型"),
    ("perspective", "视角"),
    ("game_overview", "完整游戏介绍"),
    ("gameplay", "详细玩法"),
    ("background", "公开背景"),
    ("default_pc_keybinds", "PC 默认键位"),
)

_NAMED_PC_INPUTS = {
    "Space",
    "Tab",
    "Escape",
    "Enter",
    "Backspace",
    "CapsLock",
    "LeftShift",
    "RightShift",
    "LeftCtrl",
    "RightCtrl",
    "LeftAlt",
    "RightAlt",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "MouseLeft",
    "MouseRight",
    "MouseMiddle",
    "MouseWheelUp",
    "MouseWheelDown",
    "MouseMove",
    "Backquote",
    "Minus",
    "Equals",
    "LeftBracket",
    "RightBracket",
    "Backslash",
    "Semicolon",
    "Apostrophe",
    "Comma",
    "Period",
    "Slash",
}
_SIMPLE_PC_INPUT = re.compile(r"(?:[A-Z]|[0-9]|F(?:[1-9]|1[0-9]|2[0-4])|Mouse[1-5])")
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


def _is_canonical_pc_input(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    parts = value.split("+")
    return all(
        part in _NAMED_PC_INPUTS or _SIMPLE_PC_INPUT.fullmatch(part) is not None
        for part in parts
    )


_INPUT_PATTERN = (
    r"^(?:(?:[A-Z]|[0-9]|F(?:[1-9]|1[0-9]|2[0-4])|Mouse[1-5])|"
    r"(?:Space|Tab|Escape|Enter|Backspace|CapsLock|LeftShift|RightShift|"
    r"LeftCtrl|RightCtrl|LeftAlt|RightAlt|ArrowUp|ArrowDown|ArrowLeft|"
    r"ArrowRight|MouseLeft|MouseRight|MouseMiddle|MouseWheelUp|"
    r"MouseWheelDown|MouseMove|Backquote|Minus|Equals|LeftBracket|"
    r"RightBracket|Backslash|Semicolon|Apostrophe|Comma|Period|Slash))"
    r"(?:\+(?:(?:[A-Z]|[0-9]|F(?:[1-9]|1[0-9]|2[0-4])|Mouse[1-5])|"
    r"(?:Space|Tab|Escape|Enter|Backspace|CapsLock|LeftShift|RightShift|"
    r"LeftCtrl|RightCtrl|LeftAlt|RightAlt|ArrowUp|ArrowDown|ArrowLeft|"
    r"ArrowRight|MouseLeft|MouseRight|MouseMiddle|MouseWheelUp|"
    r"MouseWheelDown|MouseMove|Backquote|Minus|Equals|LeftBracket|"
    r"RightBracket|Backslash|Semicolon|Apostrophe|Comma|Period|Slash)))*$"
)

RESPONSE_FORMAT: dict[str, object] = {
    "type": "json_schema",
    "json_schema": {
        "name": "game_knowledge_context",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [key for key, _label in ANSWER_FIELDS],
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
                        "pattern": _INPUT_PATTERN,
                    },
                },
            },
        },
    },
}

ONLINE_MODE = next(mode for mode in MODES if mode.web_enabled)
PILOT_MODES = (ONLINE_MODE,)


@dataclass(frozen=True, slots=True)
class PilotAttempt:
    game_id: str
    game_name: str
    mode_id: str
    web_enabled: bool
    requested_model: str
    requested_provider: str | None
    response_text: str
    parsed_answer: dict[str, object] | None
    format_error: str | None
    latency_seconds: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    reasoning_tokens: int | None
    cost_usd: float | None
    actual_model: str | None
    actual_provider: str | None
    finish_reason: str | None
    error: str | None
    error_metadata: dict[str, object] | None
    normalization_actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PilotRun:
    started_at: str
    finished_at: str
    attempts: tuple[PilotAttempt, ...]
    dispatch_stats: tuple[LlmDispatchStats, ...]


class PilotClient(Protocol):
    def complete_knowledge(
        self,
        *,
        model: str,
        provider: str | None,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        web_enabled: bool,
        reasoning_effort: str | None = None,
        web_search_parameters: Mapping[str, object] | None = None,
        provider_options: Mapping[str, object] | None = None,
        response_format: Mapping[str, object] | None = None,
        plugins: Sequence[Mapping[str, object]] | None = None,
    ) -> LlmResult: ...

    def dispatch_stats(self) -> LlmDispatchStats: ...

    def close(self) -> None: ...


ClientFactory = Callable[[ProbeMode], PilotClient]


def pilot_games() -> tuple[GameCase, ...]:
    return GAME_SUITES[ACTIVE_GAME_SUITE]


def render_user_prompt(game_name: str) -> str:
    return USER_PROMPT_TEMPLATE.format(game_name=game_name)


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


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


def _validate_answer(value: object) -> tuple[dict[str, object] | None, str | None]:
    if not isinstance(value, dict):
        return None, "JSON 顶层不是对象"
    expected = {key for key, _label in ANSWER_FIELDS}
    actual = set(value)
    if actual != expected:
        return None, (
            f"顶层字段不匹配：缺少 {sorted(expected - actual)}；"
            f"多出 {sorted(actual - expected)}"
        )

    genres = value["genre"]
    if (
        not isinstance(genres, list)
        or not 1 <= len(genres) <= 5
        or not all(_nonempty_string(item) for item in genres)
    ):
        return None, "genre 必须是含 1 至 5 个非空字符串的数组"
    for key in ("perspective", "game_overview"):
        if not _nonempty_string(value[key]):
            return None, f"{key} 必须是非空字符串"

    gameplay = value["gameplay"]
    gameplay_fields = {
        "player_goal",
        "core_loop",
        "major_systems",
        "modes_and_structure",
    }
    if not isinstance(gameplay, dict) or set(gameplay) != gameplay_fields:
        return None, "gameplay 字段不匹配"
    for key in ("player_goal", "core_loop", "modes_and_structure"):
        if not _nonempty_string(gameplay[key]):
            return None, f"gameplay.{key} 必须是非空字符串"
    systems = gameplay["major_systems"]
    if not isinstance(systems, list) or not 4 <= len(systems) <= 10:
        return None, "gameplay.major_systems 必须含 4 至 10 项"
    for index, item in enumerate(systems):
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "description"}
            or not all(_nonempty_string(item.get(key)) for key in item)
        ):
            return None, f"gameplay.major_systems[{index}] 字段不匹配或为空"

    background = value["background"]
    background_fields = {"setting_and_premise", "release_and_service_status"}
    if not isinstance(background, dict) or set(background) != background_fields:
        return None, "background 字段不匹配"
    if not all(_nonempty_string(background[key]) for key in background_fields):
        return None, "background 含空或非字符串值"

    keybinds = value["default_pc_keybinds"]
    if not isinstance(keybinds, dict) or len(keybinds) > 40:
        return None, "default_pc_keybinds 必须是最多 40 项的对象"
    for action, input_name in keybinds.items():
        if not _nonempty_string(action):
            return None, "default_pc_keybinds 含空动作名称"
        if not _is_canonical_pc_input(input_name):
            return None, (
                f"default_pc_keybinds[{action!r}] 不是单一规范化 PC 输入："
                f"{input_name!r}"
            )
    return value, None


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
) -> tuple[dict[str, object] | None, str | None, tuple[str, ...]]:
    repaired, comma_count = _remove_trailing_commas(text)
    try:
        value = _strict_json_loads(repaired)
    except json.JSONDecodeError as error:
        return None, (
            f"不是合法 JSON：{error.msg}（line {error.lineno}, column {error.colno}）"
        ), ()
    except (_DuplicateJsonKeyError, _NonstandardJsonConstantError) as error:
        return None, str(error), ()
    value, alias_actions = _normalize_keybind_aliases(value)
    parsed, validation_error = _validate_answer(value)
    actions: list[str] = []
    if comma_count:
        actions.append(f"移除 {comma_count} 个对象／数组尾随逗号")
    actions.extend(alias_actions)
    if parsed is None:
        return None, validation_error, tuple(actions)

    try:
        canonical_text = json.dumps(
            parsed,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        round_tripped = _strict_json_loads(canonical_text)
    except (TypeError, ValueError) as error:
        return None, f"规范 JSON 序列化失败：{error}", tuple(actions)
    round_trip_parsed, round_trip_error = _validate_answer(round_tripped)
    if round_trip_parsed != parsed or round_trip_error is not None:
        return None, "规范 JSON 二次解析未通过同一合同", tuple(actions)
    return round_trip_parsed, None, tuple(actions)


def parse_answer_detailed(
    text: str,
) -> tuple[dict[str, object] | None, str | None, tuple[str, ...]]:
    if not text.strip():
        return None, "空答", ()
    parsed, error, actions = _parse_candidate(text)
    if parsed is not None:
        warning = "；".join(actions) if actions else None
        return parsed, warning, actions

    balanced_candidates = _balanced_json_objects(text)
    if len(balanced_candidates) == 1 and balanced_candidates[0] == text.strip():
        return None, error, ()

    valid_candidates: list[
        tuple[dict[str, object], tuple[str, ...]]
    ] = []
    candidate_errors: list[str] = []
    for candidate in balanced_candidates:
        candidate_parsed, candidate_error, candidate_actions = _parse_candidate(
            candidate
        )
        if candidate_parsed is not None:
            valid_candidates.append((candidate_parsed, candidate_actions))
        elif candidate_error is not None and candidate_error not in candidate_errors:
            candidate_errors.append(candidate_error)
    if len(valid_candidates) == 1:
        extracted, candidate_actions = valid_candidates[0]
        all_actions = ("剥离 JSON 外文本／代码围栏", *candidate_actions)
        return extracted, "；".join(all_actions), all_actions
    if len(valid_candidates) > 1:
        return None, "响应中存在多个符合合同的 JSON 对象，无法确定唯一结果", ()
    if candidate_errors:
        details = "；".join(candidate_errors[:3])
        return None, f"找到完整 JSON 对象，但未通过严格合同：{details}", ()
    return None, error, ()


def parse_answer(text: str) -> tuple[dict[str, object] | None, str | None]:
    parsed, error, _actions = parse_answer_detailed(text)
    return parsed, error


def default_client_factory(mode: ProbeMode) -> PilotClient:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key is None or not key.strip():
        raise LlmError("未设置环境变量 OPENROUTER_API_KEY；无法运行 V2 pilot")
    return ProbeOpenRouterClient(
        key,
        profile_name=f"m5-b-t3a-v2:{mode.mode_id}",
        timeout_seconds=TIMEOUT_SECONDS,
    )


def run_pilot(
    *,
    client_factory: ClientFactory = default_client_factory,
    checkpoint_output: Path | None = None,
) -> PilotRun:
    started_at = datetime.now(timezone.utc).isoformat()
    clients = {mode.mode_id: client_factory(mode) for mode in PILOT_MODES}
    attempts: list[PilotAttempt] = []
    total = len(pilot_games())
    try:
        for game in pilot_games():
            for mode in PILOT_MODES:
                print(
                    f"[{len(attempts) + 1:02d}/{total}] {game.game_name} / "
                    f"{mode.mode_id}",
                    flush=True,
                )
                client = clients[mode.mode_id]
                try:
                    result = client.complete_knowledge(
                        model=MODEL,
                        provider=PROVIDER,
                        system_prompt=SYSTEM_PROMPT_V2,
                        user_prompt=render_user_prompt(game.game_name),
                        max_tokens=MAX_TOKENS,
                        temperature=TEMPERATURE,
                        web_enabled=mode.web_enabled,
                        reasoning_effort=REASONING_EFFORT,
                        web_search_parameters=WEB_SEARCH_PARAMETERS,
                        provider_options=PROVIDER_OPTIONS,
                        response_format=RESPONSE_FORMAT,
                        plugins=RESPONSE_PLUGINS,
                    )
                    parsed, format_error, normalization_actions = (
                        parse_answer_detailed(result.text)
                    )
                    if parsed is None and result.finish_reason == "length":
                        format_error = "输出因 length 截断，无法形成完整合同 JSON"
                    attempt = PilotAttempt(
                        game.game_id,
                        game.game_name,
                        mode.mode_id,
                        mode.web_enabled,
                        MODEL,
                        PROVIDER,
                        result.text,
                        parsed,
                        format_error,
                        result.latency_seconds,
                        result.usage.prompt_tokens,
                        result.usage.completion_tokens,
                        result.usage.reasoning_tokens,
                        result.usage.cost_usd,
                        result.model,
                        result.provider,
                        result.finish_reason,
                        None,
                        None,
                        normalization_actions,
                    )
                except LlmError as error:
                    attempt = PilotAttempt(
                        game.game_id,
                        game.game_name,
                        mode.mode_id,
                        mode.web_enabled,
                        MODEL,
                        PROVIDER,
                        "",
                        None,
                        None,
                        error.latency_seconds,
                        None,
                        None,
                        None,
                        None,
                        None,
                        error.provider,
                        None,
                        error.diagnostic(),
                        error.metadata(),
                    )
                attempts.append(attempt)
                checkpoint = PilotRun(
                    started_at,
                    datetime.now(timezone.utc).isoformat(),
                    tuple(attempts),
                    tuple(client.dispatch_stats() for client in clients.values()),
                )
                if checkpoint_output is not None:
                    write_raw_results(checkpoint_output, checkpoint)
        stats = tuple(client.dispatch_stats() for client in clients.values())
    finally:
        for client in clients.values():
            client.close()
    return PilotRun(
        started_at,
        datetime.now(timezone.utc).isoformat(),
        tuple(attempts),
        stats,
    )


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _metric(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def _status(attempt: PilotAttempt) -> str:
    if attempt.error is not None:
        return "失败"
    if not attempt.response_text.strip():
        return "空答"
    if attempt.parsed_answer is not None and attempt.format_error is not None:
        return "已规范化／原样不合"
    if attempt.format_error is not None:
        return "格式不合"
    return "成功"


def _cell(value: object) -> str:
    return html.escape(str(value)).replace("|", "&#124;").replace("\n", "<br>")


def render_report(run: PilotRun) -> str:
    lines = [
        f"# M5-B-T3a {MODEL_LABEL} 游戏知识探针报告",
        "",
        "本轮只验证修改后的详细游戏 context 提示词，不替换上一轮正式 3 模型 × 15 游戏判卷，也不作选型推荐。",
        "",
        "## 模型与样本",
        "",
        f"- 模型：[{MODEL_LABEL}]({MODEL_URL})（`{MODEL}`）。",
        "- 请求上游：由 OpenRouter 在当前账户数据策略允许的端点中自动选择；实际返回上游逐次记录。",
        f"- 样本：{', '.join(game.game_name for game in pilot_games())}。",
        "- 每个游戏只跑联网模式。",
        f"- 联网模式只使用网关内置 [`openrouter:web_search`]({OPENROUTER_WEB_SEARCH_DOC})，不接独立搜索 API。",
        f"- 固定参数：temperature={TEMPERATURE}，max_tokens={MAX_TOKENS}，reasoning={REASONING_EFFORT or '模型默认'}，客户端超时={TIMEOUT_SECONDS:.0f} 秒；产品延迟目标仍按 ≤{LATENCY_TARGET_SECONDS:.0f} 秒统计。",
        "- 搜索：Exa；每次最多 5 条、全请求累计最多 5 条；不限制每条结果字符数。",
        "- 路由：合规上游中按吞吐量优先；要求上游支持请求参数。",
        "- 输出：请求 OpenRouter JSON Schema 严格结构化输出；是否被实际上游执行按原始响应另行记录。",
        "- 网关响应修复：启用 OpenRouter `response-healing`；验收仍以客户端收到的 response_text 能否直接通过严格合同为准，本地规范化另列。",
        "- 这里的 httpx 超时是连接／读取等 I/O 阶段的超时，不是整次请求的总墙钟截止；联网端点持续传输数据时，实测总耗时可以明显超过 45 秒。",
        "",
        "### 样本理由",
        "",
        "| 游戏 | 类型 | 新旧／热度 | 入选理由 |",
        "|---|---|---|---|",
    ]
    for game in pilot_games():
        lines.append(
            f"| {_cell(game.game_name)} | {_cell(game.category)} | "
            f"{_cell(game.era)} | {_cell(game.reason)} |"
        )
    lines.extend([
        "",
        "## 提示词调整",
        "",
        "- 删除社区术语与 HUD 惯例。",
        "- 将一句话核心玩法扩成完整游戏介绍、详细玩法结构与不剧透的公开背景。",
        "- 默认键位统一为可机械解析的“动作名称 → 单一规范化 PC 输入”对象；移动方向与快捷栏逐键展开。",
        "- 输出面向后续每个视觉模型的稳定 context，因此完整性优先于极端短小。",
        "",
        "## 耗时与花费",
        "",
        "| 模式 | 返回 | 可机械解析 | 原样格式合规 | P50 / P90 / 最大（秒） | ≤10 秒 | 花费（USD） |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for mode in PILOT_MODES:
        attempts = [item for item in run.attempts if item.mode_id == mode.mode_id]
        returned = [item for item in attempts if item.error is None and item.response_text.strip()]
        latencies = [item.latency_seconds for item in attempts if item.latency_seconds is not None]
        valid = sum(item.parsed_answer is not None for item in attempts)
        strict = sum(
            item.parsed_answer is not None and item.format_error is None
            for item in attempts
        )
        target = sum(
            item.error is None
            and bool(item.response_text.strip())
            and item.latency_seconds is not None
            and item.latency_seconds <= LATENCY_TARGET_SECONDS
            for item in attempts
        )
        cost = sum(item.cost_usd or 0.0 for item in attempts)
        metrics = " / ".join(
            _metric(value)
            for value in (
                statistics.median(latencies) if latencies else None,
                _percentile(latencies, 0.9),
                max(latencies) if latencies else None,
            )
        )
        lines.append(
            f"| {mode.label} | {len(returned)}/{len(attempts)} | {valid}/{len(attempts)} | "
            f"{strict}/{len(attempts)} | "
            f"{metrics} | {target}/{len(attempts)} | ${cost:.9f} |"
        )
    lines.extend(
        (
            "",
            "## 逐次结果",
            "",
            "| 游戏 | 模式 | 状态 | 实际模型／上游 | 耗时（秒） | 输入／输出／推理 token | finish_reason | 花费（USD） |",
            "|---|---|---|---|---:|---:|---|---:|",
        )
    )
    for attempt in run.attempts:
        tokens = "/".join(
            "—" if value is None else str(value)
            for value in (
                attempt.prompt_tokens,
                attempt.completion_tokens,
                attempt.reasoning_tokens,
            )
        )
        cost = "—" if attempt.cost_usd is None else f"${attempt.cost_usd:.9f}"
        lines.append(
            f"| {_cell(attempt.game_name)} | {_cell(attempt.mode_id)} | {_status(attempt)} | "
            f"{_cell(attempt.actual_model or '—')} / {_cell(attempt.actual_provider or '—')} | "
            f"{_metric(attempt.latency_seconds)} | {tokens} | "
            f"{_cell(attempt.finish_reason or '—')} | {cost} |"
        )
    known_costs = [item.cost_usd for item in run.attempts if item.cost_usd is not None]
    lines.extend(
        (
            "",
            f"可归属总花费：`${sum(known_costs):.9f}`（{len(known_costs)}/{len(run.attempts)} 个调用有花费元数据）。",
            "",
            "## 限流统计",
            "",
            "| 模式 | 429 | 累计冷却（秒） | 冷却丢弃 |",
            "|---|---:|---:|---:|",
        )
    )
    mode_by_profile = {
        f"m5-b-t3a-v2:{mode.mode_id}": mode for mode in PILOT_MODES
    }
    for stat in run.dispatch_stats:
        mode = mode_by_profile[stat.profile_name]
        lines.append(
            f"| {mode.label} | {stat.rate_limit_count} | "
            f"{stat.cooldown_seconds:.3f} | {stat.cooldown_drop_count} |"
        )
    errors = [item for item in run.attempts if item.error is not None]
    lines.extend(("", "## 错误与格式", ""))
    if not errors and all(item.format_error is None for item in run.attempts):
        lines.append("- 无调用错误、空答或格式不合。")
    else:
        for attempt in run.attempts:
            detail = attempt.error or attempt.format_error
            if detail:
                lines.append(
                    f"- `{attempt.game_id}` / `{attempt.mode_id}`：{detail}"
                )
    lines.extend(
        (
            "",
            "## 说明",
            "",
            f"- 这是 {len(pilot_games())} 个游戏的小样本探针，不能替代正式跨类型判卷。",
            "- 答案未由脚本判定事实正确性；完整原文见 answers.md。",
            "- parsed-contexts.json 仅执行确定性规范化：剥离额外文本／代码围栏、移除语法上无歧义的尾随逗号、按白名单转换标点键名；不补全截断内容、不修改游戏知识。解析器拒绝任意层级重复键与 NaN/Infinity 等非标准常量；通过合同后由标准库重新序列化并二次严格解析。每一步都写入 normalization_actions，原始格式不合仍单独记录。",
            "- 详细输出与 10 秒延迟目标存在客观张力，本表保留实测，不据此自动调短提示词。",
            "",
            "## 运行信息",
            "",
            f"- 开始：`{run.started_at}`",
            f"- 结束：`{run.finished_at}`",
            "",
        )
    )
    return "\n".join(lines)


def render_answers(run: PilotRun) -> str:
    lines = [
        f"# {MODEL_LABEL} 游戏知识探针原始答案",
        "",
        "以下内容保留调用正文，仅去除行尾空白以保持报告格式；不预填事实正确性判断。精确原始字符串见 results.json。",
    ]
    for game in pilot_games():
        lines.extend(("", f"## {game.game_name}", ""))
        for mode in PILOT_MODES:
            attempt = next(
                item
                for item in run.attempts
                if item.game_id == game.game_id and item.mode_id == mode.mode_id
            )
            lines.extend((f"### {mode.label}", ""))
            if attempt.error is not None:
                lines.append(f"调用失败：{attempt.error}")
            elif not attempt.response_text:
                lines.append("（空答）")
            else:
                response_text = "\n".join(
                    line.rstrip() for line in attempt.response_text.splitlines()
                )
                lines.extend(("```json", response_text, "```"))
            lines.append("")
    return "\n".join(lines)


def render_prompt() -> str:
    return "\n".join(
        (
            "# M5-B-T3a 游戏知识线提示词 V3",
            "",
            "本轮只跑联网模式；游戏名是运行时数据。",
            "",
            "## System prompt（逐字全文）",
            "",
            "```text",
            SYSTEM_PROMPT_V2,
            "```",
            "",
            "## 用户消息模板（逐字全文）",
            "",
            "```text",
            USER_PROMPT_TEMPLATE,
            "```",
            "",
            "## Pilot 固定参数",
            "",
            f"- model：`{MODEL}`",
            "- provider：OpenRouter 自动路由（未锁定单一上游）",
            f"- temperature：`{TEMPERATURE}`",
            f"- max_tokens：`{MAX_TOKENS}`",
            f"- reasoning：`{REASONING_EFFORT or '模型默认'}`",
            f"- 客户端超时：`{TIMEOUT_SECONDS}` 秒",
            "- 联网模式：网关内置 `openrouter:web_search` Server Tool；engine=exa，max_results=5，max_total_results=5，不设置 max_characters。",
            "- provider：按 throughput 排序，require_parameters=true。",
            "- response_format：严格 JSON Schema。",
            "- plugins：`response-healing`（非流式响应的网关 JSON 验证／修复）。",
            "",
        )
    )


def _write_strict_json(path: Path, payload: object) -> None:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
    ) + "\n"
    _strict_json_loads(text)
    path.write_text(text, encoding="utf-8")


def write_raw_results(output: Path, run: PilotRun) -> None:
    output.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "model": MODEL,
        "provider": PROVIDER,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "reasoning_effort": REASONING_EFFORT,
        "web_search_parameters": WEB_SEARCH_PARAMETERS,
        "provider_options": PROVIDER_OPTIONS,
        "response_format": RESPONSE_FORMAT,
        "plugins": RESPONSE_PLUGINS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "latency_target_seconds": LATENCY_TARGET_SECONDS,
        "pilot_game_ids": [game.game_id for game in pilot_games()],
        "game_suite": ACTIVE_GAME_SUITE,
        "attempts": [asdict(item) for item in run.attempts],
        "dispatch_stats": [asdict(item) for item in run.dispatch_stats],
    }
    _write_strict_json(output / "results.json", payload)


def write_outputs(output: Path, run: PilotRun) -> None:
    write_raw_results(output, run)
    (output / "report.md").write_text(render_report(run), encoding="utf-8")
    (output / "answers.md").write_text(render_answers(run), encoding="utf-8")
    (output / "prompt-v3.md").write_text(render_prompt(), encoding="utf-8")
    parsed_payload = [
        {
            "game_id": attempt.game_id,
            "game_name": attempt.game_name,
            "context": attempt.parsed_answer,
            "format_warning": attempt.format_error,
            "normalization_actions": list(attempt.normalization_actions),
        }
        for attempt in run.attempts
    ]
    _write_strict_json(output / "parsed-contexts.json", parsed_payload)


def reparse_existing(output: Path) -> PilotRun:
    payload = json.loads((output / "results.json").read_text(encoding="utf-8"))
    attempts: list[PilotAttempt] = []
    for stored in payload["attempts"]:
        item = dict(stored)
        if item["error"] is None:
            parsed, format_error, normalization_actions = parse_answer_detailed(
                item["response_text"]
            )
            if parsed is None and item["finish_reason"] == "length":
                format_error = "输出因 length 截断，无法形成完整合同 JSON"
            item["parsed_answer"] = parsed
            item["format_error"] = format_error
            item["normalization_actions"] = normalization_actions
        else:
            item.setdefault("normalization_actions", ())
        attempts.append(PilotAttempt(**item))
    return PilotRun(
        payload["started_at"],
        payload["finished_at"],
        tuple(attempts),
        tuple(LlmDispatchStats(**item) for item in payload["dispatch_stats"]),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--model-label", default=MODEL_LABEL)
    parser.add_argument("--model-url", default=MODEL_URL)
    parser.add_argument(
        "--suite",
        choices=tuple(GAME_SUITES),
        default=ACTIVE_GAME_SUITE,
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=MAX_TOKENS,
        help="Output safety ceiling; the prompt itself has no word limit.",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("default", "none", "minimal", "low", "medium", "high", "xhigh", "max"),
        default="default",
    )
    parser.add_argument(
        "--reprocess-existing",
        action="store_true",
        help="Re-validate the saved raw responses without making model calls.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    global ACTIVE_GAME_SUITE, MODEL, MODEL_LABEL, MODEL_URL, MAX_TOKENS, REASONING_EFFORT
    ACTIVE_GAME_SUITE = arguments.suite
    MODEL = arguments.model
    MODEL_LABEL = arguments.model_label
    MODEL_URL = arguments.model_url
    if arguments.max_tokens <= 0:
        raise SystemExit("--max-tokens must be positive")
    MAX_TOKENS = arguments.max_tokens
    REASONING_EFFORT = (
        None if arguments.reasoning_effort == "default" else arguments.reasoning_effort
    )
    run = (
        reparse_existing(arguments.output)
        if arguments.reprocess_existing
        else run_pilot(checkpoint_output=arguments.output)
    )
    write_outputs(arguments.output, run)
    print(
        json.dumps(
            {
                "attempts": len(run.attempts),
                "output": str(arguments.output.resolve()),
                "cost_usd": sum(item.cost_usd or 0.0 for item in run.attempts),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
