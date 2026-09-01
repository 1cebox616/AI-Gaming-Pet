"""Compare three game-knowledge models with one prompt and blank human grading."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import statistics
import time
from typing import Protocol

import httpx

from pet.core.llm import (
    LlmDispatchStats,
    LlmError,
    LlmResult,
    OpenRouterClient,
    _parse_result,
    _response_llm_error,
)


BACKEND_DIRECTORY = Path(__file__).resolve().parents[5]
DEFAULT_OUTPUT = BACKEND_DIRECTORY / "eval-reports" / "m5-b-t3a"
OPENROUTER_WEB_SEARCH_DOC = (
    "https://openrouter.ai/docs/guides/features/server-tools/web-search"
)
TEMPERATURE = 0.0
TIMEOUT_SECONDS = 10.0
MAX_TOKENS = 900


SYSTEM_PROMPT = """你是“游戏知识线”的公开资料整理器。只回答玩家在开始游玩前可以公开查到的通用知识。

若调用环境提供联网工具，先用它核查当前公开资料；若没有联网工具，则使用自身知识。不得把不确定内容猜成事实；不知道时写“不确定”。不要提供剧情、角色命运、结局、具体地图内容或世界探索内容。

只输出一个合法 JSON 对象，不要 Markdown、代码围栏、引用列表或额外说明。顶层字段必须恰好如下：
{
  "genre": "简短类型",
  "perspective": "简短视角",
  "core_gameplay": "一句话核心玩法",
  "hud_conventions": [
    {"element": "通常显示的界面元素", "usual_position": "通常位置"}
  ],
  "default_keybinds": [
    {"action": "核心动作", "input": "默认按键或控制器输入", "platform": "对应平台与输入设备"}
  ],
  "community_terms": [
    {"term": "社区常用术语", "meaning": "简短含义"}
  ]
}

约束：
- hud_conventions 最多 5 项；只写惯例，不声称看见了任何实际画面。
- default_keybinds 最多 8 项；平台存在差异时明确 platform，不做键位印证。
- community_terms 最多 5 项。
- 所有字符串简短、可单独判定对／错／不确定。"""

USER_PROMPT_TEMPLATE = "游戏名称：{game_name}"

ANSWER_FIELDS = (
    ("genre", "类型"),
    ("perspective", "视角"),
    ("core_gameplay", "一句话核心玩法"),
    ("hud_conventions", "HUD 惯例"),
    ("default_keybinds", "核心动作默认键位"),
    ("community_terms", "社区常用术语"),
)


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    label: str
    model: str
    provider: str
    input_price_per_million_usd: float
    output_price_per_million_usd: float
    tier: str
    reason: str
    source_url: str


CANDIDATES = (
    Candidate(
        candidate_id="qwen38-flash",
        label="Qwen3.8 Flash",
        model="qwen/qwen3.8-flash",
        provider="Alibaba",
        input_price_per_million_usd=0.15,
        output_price_per_million_usd=0.47,
        tier="低价／新模型基线",
        reason=(
            "现有场景命名线同款，价格最低且发布新；用于测量低成本快速档是否已足够。"
        ),
        source_url="https://openrouter.ai/qwen/qwen3.8-flash",
    ),
    Candidate(
        candidate_id="gpt54-mini",
        label="GPT-5.4 Mini",
        model="openai/gpt-5.4-mini",
        provider="Azure",
        input_price_per_million_usd=0.75,
        output_price_per_million_usd=4.50,
        tier="中价／均衡档",
        reason=(
            "价格与能力位于两端候选之间，官方标注知识截止到 2025-08，适合直接观察联网对 2026 新作的增益。"
        ),
        source_url="https://openrouter.ai/openai/gpt-5.4-mini",
    ),
    Candidate(
        candidate_id="claude-sonnet46",
        label="Claude Sonnet 4.6",
        model="anthropic/claude-sonnet-4.6",
        provider="Anthropic",
        input_price_per_million_usd=3.00,
        output_price_per_million_usd=15.00,
        tier="高价／能力上限",
        reason=(
            "价格显著最高的强能力对照；用于判断更高档模型在跨类型、结构遵循和长尾游戏知识上是否带来可见收益。"
        ),
        source_url="https://openrouter.ai/anthropic/claude-sonnet-4.6",
    ),
)


@dataclass(frozen=True, slots=True)
class GameCase:
    game_id: str
    game_name: str
    display_name: str
    category: str
    era: str
    reason: str
    source_url: str | None = None


GAMES = (
    GameCase(
        "overwatch-2",
        "Overwatch 2",
        "守望先锋 2",
        "英雄射击",
        "热门长线更新",
        "项目有录像；热门团队射击可检验角色分工、HUD 与团队术语。",
    ),
    GameCase(
        "dont-starve-together",
        "Don't Starve Together",
        "饥荒联机版",
        "生存／制作",
        "经典长线更新",
        "项目有录像；老牌合作生存游戏可检验制作、季节与多人术语。",
    ),
    GameCase(
        "gray-zone-warfare",
        "Gray Zone Warfare",
        "Gray Zone Warfare",
        "战术撤离射击",
        "较新抢先体验",
        "项目有录像；较新的硬核射击可检验长尾知识和复杂操作约定。",
    ),
    GameCase(
        "slay-the-spire-2",
        "Slay the Spire 2",
        "杀戮尖塔 2",
        "卡牌／Roguelike",
        "2026-03-05 抢先体验",
        "项目有录像；2026 新作，是知识时效与联网价值的直接样本。",
        "https://www.megacrit.com/news/2026-02-19-release-date-trailer/",
    ),
    GameCase(
        "league-of-legends",
        "League of Legends",
        "英雄联盟",
        "MOBA",
        "热门长线更新",
        "高热度竞技游戏，UI、默认操作与社区术语密集。",
    ),
    GameCase(
        "baldurs-gate-3",
        "Baldur's Gate 3",
        "博德之门 3",
        "回合制 CRPG",
        "2023 热门作品",
        "检验队伍制角色扮演、回合制操作和复杂界面惯例。",
    ),
    GameCase(
        "civilization-vii",
        "Sid Meier's Civilization VII",
        "文明 7",
        "4X 策略",
        "2025 新作",
        "检验宏观策略、多层 UI 与策略社区术语。",
    ),
    GameCase(
        "forza-horizon-6",
        "Forza Horizon 6",
        "极限竞速：地平线 6",
        "开放世界竞速",
        "2026-05-19 发售",
        "2026 新作；检验竞速视角、手柄操作与联网时效。",
        "https://forza.net/news/forza-horizon-6-coming-may-2026",
    ),
    GameCase(
        "mario-tennis-fever",
        "Mario Tennis Fever",
        "马力欧网球 狂热",
        "街机体育",
        "2026-02-12 发售",
        "2026 新作且为主机独占；检验体育计分 UI 与控制器键位。",
        "https://www.nintendo.com/en-gb/Games/Nintendo-Switch-2-games/Mario-Tennis-Fever-2915160.html",
    ),
    GameCase(
        "street-fighter-6",
        "Street Fighter 6",
        "街头霸王 6",
        "格斗",
        "2023 热门长线更新",
        "检验格斗输入、回合 HUD 与高度专门化的社区术语。",
    ),
    GameCase(
        "microsoft-flight-simulator-2024",
        "Microsoft Flight Simulator 2024",
        "微软模拟飞行 2024",
        "飞行模拟",
        "2024 专业模拟",
        "检验复杂模拟类型、多设备输入和仪表／HUD 边界。",
    ),
    GameCase(
        "hollow-knight-silksong",
        "Hollow Knight: Silksong",
        "空洞骑士：丝之歌",
        "平台动作／类银河战士恶魔城",
        "2025 新作",
        "检验横版视角、平台动作与较新作品知识。",
    ),
    GameCase(
        "blue-prince",
        "Blue Prince",
        "蓝途王子",
        "解谜／Roguelike",
        "2025 新作",
        "较新独立解谜作品，检验长尾类型与非标准 UI。",
    ),
    GameCase(
        "resident-evil-requiem",
        "Resident Evil Requiem",
        "生化危机：安魂曲",
        "生存恐怖",
        "2026-02-27 发售",
        "2026 新作；检验恐怖游戏惯例与知识时效，且明确排除剧情内容。",
        "https://www.capcom.co.jp/ir/english/news/html/e250609.html",
    ),
    GameCase(
        "final-fantasy-xiv-online",
        "FINAL FANTASY XIV Online",
        "最终幻想 14",
        "MMORPG",
        "热门长线更新",
        "补足大型多人在线类型，检验热键栏、职业与团队社区术语。",
    ),
)


@dataclass(frozen=True, slots=True)
class ProbeMode:
    mode_id: str
    label: str
    web_enabled: bool


MODES = (
    ProbeMode("knowledge", "知识模式", False),
    ProbeMode("online", "联网模式", True),
)


@dataclass(frozen=True, slots=True)
class Attempt:
    game_id: str
    game_name: str
    candidate_id: str
    requested_model: str
    requested_provider: str
    mode_id: str
    web_enabled: bool
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

    @property
    def returned(self) -> bool:
        return self.error is None and bool(self.response_text.strip())

    @property
    def cooldown_dropped(self) -> bool:
        return bool(self.error_metadata and self.error_metadata.get("cooldown_drop"))


@dataclass(frozen=True, slots=True)
class ProbeRun:
    started_at: str
    finished_at: str
    attempts: tuple[Attempt, ...]
    dispatch_stats: tuple[LlmDispatchStats, ...]
    deviations: tuple[str, ...] = ()
    prior_unpersisted_call_count: int = 0


@dataclass(frozen=True, slots=True)
class ProbeSeed:
    attempts: tuple[Attempt, ...]
    dispatch_stats: tuple[LlmDispatchStats, ...]
    deviations: tuple[str, ...]
    prior_unpersisted_call_count: int


class ProbeClient(Protocol):
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
    ) -> LlmResult: ...

    def dispatch_stats(self) -> LlmDispatchStats: ...

    def close(self) -> None: ...


class ProbeOpenRouterClient(OpenRouterClient):
    """Evaluation-only server-tool request while retaining T7.8 cooldown."""

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
    ) -> LlmResult:
        if not web_enabled:
            return self.complete(
                model=model,
                provider=provider,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
            )
        self._cooldown.before_dispatch()
        started_at = self._clock()
        request_body: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
            "tools": [
                {
                    "type": "openrouter:web_search",
                    "parameters": dict(
                        web_search_parameters or {"max_results": 3}
                    ),
                }
            ],
        }
        if provider is not None:
            if provider_options is not None:
                raise ValueError(
                    "provider and provider_options cannot both be supplied"
                )
            request_body["provider"] = {
                "only": [provider],
                "allow_fallbacks": False,
            }
        elif provider_options is not None:
            request_body["provider"] = dict(provider_options)
        if reasoning_effort is not None:
            request_body["reasoning"] = {"effort": reasoning_effort}
        if response_format is not None:
            request_body["response_format"] = dict(response_format)
        try:
            response = self._client.post("chat/completions", json=request_body)
        except httpx.TimeoutException as error:
            raise LlmError(
                f"OpenRouter 联网请求超时：{error}",
                latency_seconds=self._clock() - started_at,
                profile_name=self._profile_name,
                provider=provider,
            ) from error
        except httpx.RequestError as error:
            raise LlmError(
                f"OpenRouter 联网请求失败：{error}",
                latency_seconds=self._clock() - started_at,
                profile_name=self._profile_name,
                provider=provider,
            ) from error
        latency = self._clock() - started_at
        if not response.is_success:
            error = _response_llm_error(
                response,
                service_label="OpenRouter",
                latency_seconds=latency,
                fallback_provider=provider,
                profile_name=self._profile_name,
            )
            if error.status_code == 429:
                self._cooldown.enter(error)
            raise error
        self._cooldown.record_success()
        try:
            payload: object = response.json()
            return _parse_result(payload, latency_seconds=latency)
        except (TypeError, ValueError, KeyError, IndexError) as error:
            raise LlmError(
                f"OpenRouter 返回了无法解析的联网成功响应：{error}",
                status_code=response.status_code,
                latency_seconds=latency,
                profile_name=self._profile_name,
                provider=provider,
            ) from error


ClientFactory = Callable[[Candidate, ProbeMode], ProbeClient]
Checkpoint = Callable[[ProbeRun], None]


def render_user_prompt(game_name: str) -> str:
    return USER_PROMPT_TEMPLATE.format(game_name=game_name)


def parse_answer(text: str) -> tuple[dict[str, object] | None, str | None]:
    if not text.strip():
        return None, "空答"
    try:
        value: object = json.loads(text)
    except json.JSONDecodeError as error:
        return None, f"不是合法 JSON：{error.msg}（line {error.lineno}, column {error.colno}）"
    if not isinstance(value, dict):
        return None, "JSON 顶层不是对象"
    expected = {key for key, _label in ANSWER_FIELDS}
    actual = set(value)
    if actual != expected:
        return None, f"顶层字段不匹配：缺少 {sorted(expected - actual)}；多出 {sorted(actual - expected)}"
    for key in ("genre", "perspective", "core_gameplay"):
        if not isinstance(value[key], str) or not value[key].strip():
            return None, f"{key} 必须是非空字符串"
    list_specs = {
        "hud_conventions": ("element", "usual_position", 5),
        "default_keybinds": ("action", "input", "platform", 8),
        "community_terms": ("term", "meaning", 5),
    }
    for key, (*required, maximum) in list_specs.items():
        items = value[key]
        if not isinstance(items, list):
            return None, f"{key} 必须是数组"
        if len(items) > maximum:
            return None, f"{key} 超过 {maximum} 项"
        for index, item in enumerate(items):
            if not isinstance(item, dict) or set(item) != set(required):
                return None, f"{key}[{index}] 字段不匹配"
            if any(not isinstance(item[field], str) or not item[field].strip() for field in required):
                return None, f"{key}[{index}] 含空或非字符串值"
    return value, None


def parse_display_answer(text: str) -> dict[str, object] | None:
    """Extract exact JSON from a fenced answer for aligned judging only.

    The strict format result remains unchanged. This does not repair, truncate,
    or reinterpret model content; it only removes one surrounding Markdown
    fence so the already-structured fields can be displayed side by side.
    """
    parsed, error = parse_answer(text)
    if error is None:
        return parsed
    if not text.startswith("```json\n") or not text.endswith("\n```"):
        return None
    inner = text.removeprefix("```json\n").removesuffix("\n```")
    parsed, error = parse_answer(inner)
    if error is None:
        return parsed
    parsed, error = parse_answer(_escape_display_only_inner_quotes(inner))
    return parsed if error is None else None


def _escape_display_only_inner_quotes(text: str) -> str:
    """Escape obvious unescaped quotes inside JSON strings for display only."""
    output: list[str] = []
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if not in_string:
            output.append(character)
            if character == '"':
                in_string = True
            continue
        if escaped:
            output.append(character)
            escaped = False
            continue
        if character == "\\":
            output.append(character)
            escaped = True
            continue
        if character != '"':
            output.append(character)
            continue
        following = text[index + 1 :]
        next_nonspace = next(
            (item for item in following if not item.isspace()), ""
        )
        if next_nonspace in {":", ",", "]", "}"}:
            output.append(character)
            in_string = False
        else:
            output.append('\\"')
    return "".join(output)


def default_client_factory(candidate: Candidate, mode: ProbeMode) -> ProbeClient:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key is None or not key.strip():
        raise LlmError("未设置环境变量 OPENROUTER_API_KEY；无法运行知识探针")
    return ProbeOpenRouterClient(
        key,
        profile_name=f"m5-b-t3a:{candidate.candidate_id}:{mode.mode_id}",
        timeout_seconds=TIMEOUT_SECONDS,
    )


def _attempt_key(attempt: Attempt) -> tuple[str, str, str]:
    return attempt.game_id, attempt.candidate_id, attempt.mode_id


def _profile_name(candidate: Candidate, mode: ProbeMode) -> str:
    return f"m5-b-t3a:{candidate.candidate_id}:{mode.mode_id}"


def _merge_dispatch_stats(
    live: Sequence[LlmDispatchStats],
    seeded: Sequence[LlmDispatchStats],
) -> tuple[LlmDispatchStats, ...]:
    seeded_by_profile = {item.profile_name: item for item in seeded}
    unknown = set(seeded_by_profile) - {item.profile_name for item in live}
    if unknown:
        raise ValueError(f"seed contains unknown dispatch profiles: {sorted(unknown)}")
    combined: list[LlmDispatchStats] = []
    for item in live:
        earlier = seeded_by_profile.get(item.profile_name)
        if earlier is None:
            combined.append(item)
            continue
        combined.append(
            LlmDispatchStats(
                profile_name=item.profile_name,
                rate_limit_count=(
                    earlier.rate_limit_count + item.rate_limit_count
                ),
                cooldown_seconds=(
                    earlier.cooldown_seconds + item.cooldown_seconds
                ),
                cooldown_drop_count=(
                    earlier.cooldown_drop_count + item.cooldown_drop_count
                ),
                cooling_down=item.cooling_down,
                cooldown_remaining_seconds=item.cooldown_remaining_seconds,
            )
        )
    return tuple(combined)


def load_seed(path: Path) -> ProbeSeed:
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("seed root must be an object")
    attempts_value = payload.get("attempts")
    stats_value = payload.get("dispatch_stats")
    deviations_value = payload.get("deviations", [])
    prior_value = payload.get("prior_unpersisted_call_count", 0)
    if not isinstance(attempts_value, list) or not isinstance(stats_value, list):
        raise ValueError("seed must contain attempts and dispatch_stats arrays")
    if not isinstance(deviations_value, list) or not all(
        isinstance(item, str) and item.strip() for item in deviations_value
    ):
        raise ValueError("seed deviations must be non-empty strings")
    if not isinstance(prior_value, int) or isinstance(prior_value, bool) or prior_value < 0:
        raise ValueError("seed prior_unpersisted_call_count must be non-negative")
    attempts = tuple(Attempt(**item) for item in attempts_value)
    stats = tuple(LlmDispatchStats(**item) for item in stats_value)
    expected_keys = {
        (game.game_id, candidate.candidate_id, mode.mode_id)
        for game in GAMES
        for candidate in CANDIDATES
        for mode in MODES
    }
    keys = [_attempt_key(item) for item in attempts]
    if len(keys) != len(set(keys)):
        raise ValueError("seed contains duplicate logical attempts")
    if not set(keys) <= expected_keys:
        raise ValueError("seed contains an unknown game/model/mode attempt")
    return ProbeSeed(
        attempts=attempts,
        dispatch_stats=stats,
        deviations=tuple(deviations_value),
        prior_unpersisted_call_count=prior_value,
    )


def load_run(path: Path) -> ProbeRun:
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("run root must be an object")
    started_at = payload.get("started_at")
    finished_at = payload.get("finished_at")
    attempts_value = payload.get("attempts")
    stats_value = payload.get("dispatch_stats")
    deviations_value = payload.get("deviations", [])
    prior_value = payload.get("prior_unpersisted_call_count", 0)
    if not isinstance(started_at, str) or not isinstance(finished_at, str):
        raise ValueError("run must contain string timestamps")
    if not isinstance(attempts_value, list) or not isinstance(stats_value, list):
        raise ValueError("run must contain attempts and dispatch_stats arrays")
    if not isinstance(deviations_value, list) or not all(
        isinstance(item, str) and item.strip() for item in deviations_value
    ):
        raise ValueError("run deviations must be non-empty strings")
    if not isinstance(prior_value, int) or isinstance(prior_value, bool) or prior_value < 0:
        raise ValueError("run prior_unpersisted_call_count must be non-negative")
    return ProbeRun(
        started_at=started_at,
        finished_at=finished_at,
        attempts=tuple(Attempt(**item) for item in attempts_value),
        dispatch_stats=tuple(LlmDispatchStats(**item) for item in stats_value),
        deviations=tuple(deviations_value),
        prior_unpersisted_call_count=prior_value,
    )


def run_probe(
    *,
    client_factory: ClientFactory = default_client_factory,
    seed: ProbeSeed | None = None,
    checkpoint: Checkpoint | None = None,
) -> ProbeRun:
    started = datetime.now(timezone.utc).isoformat()
    selected_seed = seed or ProbeSeed((), (), (), 0)
    seeded_attempts = {_attempt_key(item): item for item in selected_seed.attempts}
    clients = {
        (candidate.candidate_id, mode.mode_id): client_factory(candidate, mode)
        for candidate in CANDIDATES
        for mode in MODES
    }
    attempts: list[Attempt] = []
    try:
        total = len(GAMES) * len(CANDIDATES) * len(MODES)
        for game in GAMES:
            for mode in MODES:
                for candidate in CANDIDATES:
                    logical_key = (game.game_id, candidate.candidate_id, mode.mode_id)
                    seeded_attempt = seeded_attempts.get(logical_key)
                    if seeded_attempt is not None:
                        attempts.append(seeded_attempt)
                        print(
                            f"[{len(attempts):02d}/{total}] {game.game_name} / "
                            f"{candidate.candidate_id} / {mode.mode_id} / seeded",
                            flush=True,
                        )
                        continue
                    client = clients[(candidate.candidate_id, mode.mode_id)]
                    ordinal = len(attempts) + 1
                    print(
                        f"[{ordinal:02d}/{total}] {game.game_name} / "
                        f"{candidate.candidate_id} / {mode.mode_id}",
                        flush=True,
                    )
                    try:
                        result = client.complete_knowledge(
                            model=candidate.model,
                            provider=candidate.provider,
                            system_prompt=SYSTEM_PROMPT,
                            user_prompt=render_user_prompt(game.game_name),
                            max_tokens=MAX_TOKENS,
                            temperature=TEMPERATURE,
                            web_enabled=mode.web_enabled,
                        )
                        parsed, format_error = parse_answer(result.text)
                        attempts.append(
                            Attempt(
                                game.game_id,
                                game.game_name,
                                candidate.candidate_id,
                                candidate.model,
                                candidate.provider,
                                mode.mode_id,
                                mode.web_enabled,
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
                            )
                        )
                    except LlmError as error:
                        attempts.append(
                            Attempt(
                                game.game_id,
                                game.game_name,
                                candidate.candidate_id,
                                candidate.model,
                                candidate.provider,
                                mode.mode_id,
                                mode.web_enabled,
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
                        )
                    if checkpoint is not None:
                        live_stats = tuple(
                            clients[(item.candidate_id, item_mode.mode_id)].dispatch_stats()
                            for item in CANDIDATES
                            for item_mode in MODES
                        )
                        checkpoint(
                            ProbeRun(
                                started_at=started,
                                finished_at=datetime.now(timezone.utc).isoformat(),
                                attempts=tuple(attempts),
                                dispatch_stats=_merge_dispatch_stats(
                                    live_stats, selected_seed.dispatch_stats
                                ),
                                deviations=selected_seed.deviations,
                                prior_unpersisted_call_count=(
                                    selected_seed.prior_unpersisted_call_count
                                ),
                            )
                        )
        stats = tuple(
            clients[(candidate.candidate_id, mode.mode_id)].dispatch_stats()
            for candidate in CANDIDATES
            for mode in MODES
        )
    finally:
        for client in clients.values():
            client.close()
    return ProbeRun(
        started_at=started,
        finished_at=datetime.now(timezone.utc).isoformat(),
        attempts=tuple(attempts),
        dispatch_stats=_merge_dispatch_stats(stats, selected_seed.dispatch_stats),
        deviations=selected_seed.deviations,
        prior_unpersisted_call_count=selected_seed.prior_unpersisted_call_count,
    )


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _attempts_for(
    run: ProbeRun, candidate: Candidate, mode: ProbeMode
) -> tuple[Attempt, ...]:
    return tuple(
        attempt
        for attempt in run.attempts
        if attempt.candidate_id == candidate.candidate_id
        and attempt.mode_id == mode.mode_id
    )


def _metric(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _status(attempt: Attempt) -> str:
    if attempt.cooldown_dropped:
        return "冷却丢弃"
    if attempt.error is not None:
        return "失败"
    if not attempt.response_text.strip():
        return "空答"
    if attempt.format_error is not None:
        return "格式不合"
    return "成功"


def render_report(run: ProbeRun) -> str:
    lines = [
        "# M5-B-T3a 游戏知识线模型选型探针报告",
        "",
        "本报告只列机器测量与原始答复状态，不作选型推荐；答案正确性留给产品负责人在 `judging.md` 判定。",
        "",
        "## 候选与入围理由",
        "",
        "| 候选 | 模型 ID | 请求锁定上游 | 档次 | 标价（输入／输出，每百万 token） | 入围理由 |",
        "|---|---|---|---|---:|---|",
    ]
    for candidate in CANDIDATES:
        lines.append(
            f"| [{candidate.label}]({candidate.source_url}) | `{candidate.model}` | "
            f"`{candidate.provider}` | {candidate.tier} | "
            f"${candidate.input_price_per_million_usd:g} / ${candidate.output_price_per_million_usd:g} | "
            f"{candidate.reason} |"
        )
    lines.extend(
        (
            "",
            "三个候选覆盖约 20 倍输入标价、约 32 倍输出标价，并包含低价新模型、中档均衡模型和高价能力上限。每个请求都设置一个上游并令 `allow_fallbacks=false`；实际返回上游逐次记录，若网关没有遵守请求限制则以实际值为准。",
            "",
            "## 模式与联网依据",
            "",
            "- 知识模式：不提供任何联网工具，只用模型自身知识。",
            f"- 联网模式：OpenRouter 官方说明其 [`openrouter:web_search` Server Tool]({OPENROUTER_WEB_SEARCH_DOC}) 可用于任意模型；本探针向每次联网请求提供该网关内置工具，最多 3 个结果。没有接独立搜索 API。",
            "- 两种模式、三个模型使用完全相同的 system prompt 与用户消息模板；唯一按题变化的数据是游戏名称。温度固定为 0，`max_tokens=900`，客户端超时配置为 10 秒。",
            "- 联网工具由模型决定是否调用；“联网模式”表示工具可用，不把是否实际搜索伪装成已知事实。",
            "",
            "## 15 款游戏与入选理由",
            "",
            "| # | 游戏 | 类型 | 新旧／热度 | 入选理由 |",
            "|---:|---|---|---|---|",
        )
    )
    for index, game in enumerate(GAMES, start=1):
        game_label = (
            f"[{game.game_name}]({game.source_url})"
            if game.source_url is not None
            else game.game_name
        )
        lines.append(
            f"| {index} | {game_label}（{game.display_name}） | {game.category} | "
            f"{game.era} | {game.reason} |"
        )
    lines.extend(
        (
            "",
            "其中 Slay the Spire 2、Forza Horizon 6、Mario Tennis Fever、Resident Evil Requiem 均为 2026 年发售或进入抢先体验，超过“至少 3 款 2025 年之后发售或更新”的要求。",
            "",
            "## 模型 × 模式耗时、返回率与花费",
            "",
            "耗时分布统计所有真正派发且返回了延迟元数据的调用（含失败）；冷却期本地丢弃没有网络耗时，不混入分布。`≤10 秒`比例的分母固定为计划的 15 题，失败、空答和冷却丢弃均不计达标。",
            "",
            "| 模型 | 模式 | 返回／15 | P50 / P90 / 最大（秒） | ≤10 秒 | 已报告花费调用 | 总花费（USD） |",
            "|---|---|---:|---:|---:|---:|---:|",
        )
    )
    for candidate in CANDIDATES:
        for mode in MODES:
            attempts = _attempts_for(run, candidate, mode)
            latencies = [
                item.latency_seconds
                for item in attempts
                if item.latency_seconds is not None
            ]
            returned = sum(item.returned for item in attempts)
            within = sum(
                item.returned
                and item.latency_seconds is not None
                and item.latency_seconds <= TIMEOUT_SECONDS
                for item in attempts
            )
            costs = [item.cost_usd for item in attempts if item.cost_usd is not None]
            lines.append(
                f"| {candidate.label} | {mode.label} | {returned}/15 | "
                f"{_metric(_percentile(latencies, 0.5))} / "
                f"{_metric(_percentile(latencies, 0.9))} / "
                f"{_metric(max(latencies) if latencies else None)} | "
                f"{within}/15（{within / 15:.1%}） | {len(costs)}/15 | "
                f"${sum(costs):.6f} |"
            )
    lines.extend(
        (
            "",
            "## 限流统计",
            "",
            "T7.8 冷却按 `模型 × 模式` 六个独立档位隔离；429 后不重试，仍处于冷却的后续题在编码／派发前直接丢弃。退避起点 1 秒与上限 60 秒仍是待实测保守占位，本报告不把它称为已调优参数。",
            "",
            "| 模型 | 模式 | 429 次数 | 累计冷却时长（秒） | 冷却丢弃 | 结束时仍冷却 |",
            "|---|---|---:|---:|---:|---|",
        )
    )
    stat_by_profile = {item.profile_name: item for item in run.dispatch_stats}
    for candidate in CANDIDATES:
        for mode in MODES:
            profile = f"m5-b-t3a:{candidate.candidate_id}:{mode.mode_id}"
            stat = stat_by_profile[profile]
            lines.append(
                f"| {candidate.label} | {mode.label} | {stat.rate_limit_count} | "
                f"{stat.cooldown_seconds:.3f} | {stat.cooldown_drop_count} | "
                f"{'是' if stat.cooling_down else '否'} |"
            )
    lines.append(
        f"| **合计** |  | **{sum(item.rate_limit_count for item in run.dispatch_stats)}** | "
        f"**{sum(item.cooldown_seconds for item in run.dispatch_stats):.3f}** | "
        f"**{sum(item.cooldown_drop_count for item in run.dispatch_stats)}** |  |"
    )
    lines.extend(
        (
            "",
            "## 失败、空答与格式",
            "",
            "| 模型 | 模式 | 失败 | 空答 | 格式不合 | 截断 | 成功且格式合规 |",
            "|---|---|---:|---:|---:|---:|---:|",
        )
    )
    for candidate in CANDIDATES:
        for mode in MODES:
            attempts = _attempts_for(run, candidate, mode)
            failed = sum(item.error is not None for item in attempts)
            empty = sum(item.error is None and not item.response_text.strip() for item in attempts)
            malformed = sum(
                item.format_error is not None and bool(item.response_text.strip())
                for item in attempts
            )
            truncated = sum(item.finish_reason == "length" for item in attempts)
            valid = sum(item.parsed_answer is not None for item in attempts)
            lines.append(
                f"| {candidate.label} | {mode.label} | {failed} | {empty} | "
                f"{malformed} | {truncated} | {valid} |"
            )
    lines.extend(
        (
            "",
            "## 每次调用的耗时与花费",
            "",
            "| # | 游戏 | 模型 | 模式 | 状态 | 实际模型／上游 | 耗时（秒） | 输入／输出／推理 token | 花费（USD） |",
            "|---:|---|---|---|---|---|---:|---:|---:|",
        )
    )
    candidate_by_id = {item.candidate_id: item for item in CANDIDATES}
    mode_by_id = {item.mode_id: item for item in MODES}
    for index, attempt in enumerate(run.attempts, start=1):
        candidate = candidate_by_id[attempt.candidate_id]
        mode = mode_by_id[attempt.mode_id]
        tokens = "/".join(
            "—" if value is None else str(value)
            for value in (
                attempt.prompt_tokens,
                attempt.completion_tokens,
                attempt.reasoning_tokens,
            )
        )
        cost = "—" if attempt.cost_usd is None else f"${attempt.cost_usd:.9f}"
        actual = f"{attempt.actual_model or '—'} / {attempt.actual_provider or '—'}"
        lines.append(
            f"| {index} | {attempt.game_name} | {candidate.label} | {mode.label} | "
            f"{_status(attempt)} | {actual} | {_metric(attempt.latency_seconds)} | "
            f"{tokens} | {cost} |"
        )
    known_costs = [item.cost_usd for item in run.attempts if item.cost_usd is not None]
    lines.extend(
        (
            f"| **合计** | **{len(run.attempts)} 次计划调用** |  |  |  |  |  |  | **${sum(known_costs):.9f}** |",
            "",
            f"花费元数据覆盖 {len(known_costs)}/{len(run.attempts)} 个逻辑题格；合计只累加本次可恢复且由上游明确报告的花费，不用标价反推缺失值。",
            "",
            "## 错误明细",
            "",
        )
    )
    errors = [attempt for attempt in run.attempts if attempt.error is not None]
    if not errors:
        lines.append("- 无调用错误。")
    else:
        for attempt in errors:
            lines.append(
                f"- `{attempt.game_id}` / `{attempt.candidate_id}` / `{attempt.mode_id}`：{attempt.error}"
            )
    lines.extend(
        (
            "",
            "## 与规格的偏差",
            "",
        )
    )
    if run.deviations:
        lines.extend(f"- {item}" for item in run.deviations)
    else:
        lines.append("- 无。")
    provider_mismatches = [
        item
        for item in run.attempts
        if item.actual_provider is not None
        and item.actual_provider.casefold() != item.requested_provider.casefold()
    ]
    if provider_mismatches:
        grouped = sorted(
            {
                (
                    item.candidate_id,
                    item.mode_id,
                    item.requested_provider,
                    item.actual_provider or "",
                )
                for item in provider_mismatches
            }
        )
        detail = "；".join(
            f"{candidate_id}/{mode_id} 请求 {requested}、实际 {actual}"
            for candidate_id, mode_id, requested, actual in grouped
        )
        lines.append(
            f"- 上游锁定未完全生效：{detail}。相关跨模式延迟同时包含上游差异，不能只归因于联网工具。"
        )
    lines.extend(
        (
            "",
            "## 未完成项",
            "",
            (
                "- 首批未持久化调用的逐次答案、耗时、token 与花费无法从网关恢复；"
                "本报告仅统计恢复批可归属花费。"
                if run.prior_unpersisted_call_count
                else "- 无。"
            ),
            "",
            "## 运行信息",
            "",
            f"- 开始：`{run.started_at}`",
            f"- 结束：`{run.finished_at}`",
            f"- 计划调用：{len(GAMES)} 游戏 × {len(CANDIDATES)} 模型 × {len(MODES)} 模式 = {len(run.attempts)}。",
            (
                f"- 报告外、无法逐次归属的首批调用：{run.prior_unpersisted_call_count}。"
                if run.prior_unpersisted_call_count
                else "- 报告外调用：无。"
            ),
            "",
        )
    )
    return "\n".join(lines)


def _markdown_cell(value: object) -> str:
    return html.escape(str(value)).replace("|", "&#124;")


def _answer_cell(attempt: Attempt, field: str) -> str:
    if attempt.error is not None:
        return f"（{_status(attempt)}：{_markdown_cell(attempt.error)}）"
    if not attempt.response_text.strip():
        return "（空答）"
    display_answer = attempt.parsed_answer or parse_display_answer(
        attempt.response_text
    )
    if display_answer is None:
        return "（格式不合；原文见表后）"
    value = display_answer[field]
    prefix = "（格式不合：机械容错拆栏；原文保留）<br>" if attempt.parsed_answer is None else ""
    if isinstance(value, str):
        return prefix + _markdown_cell(value)
    if isinstance(value, list):
        rendered = []
        for item in value:
            if isinstance(item, Mapping):
                rendered.append(
                    "；".join(
                        f"{_markdown_cell(key)}：{_markdown_cell(item_value)}"
                        for key, item_value in item.items()
                    )
                )
        content = "<br>".join(rendered) if rendered else "（空数组）"
        return prefix + content
    return prefix + _markdown_cell(value)


def render_judging(run: ProbeRun) -> str:
    lines = [
        "# M5-B-T3a 游戏知识线人工判卷表",
        "",
        "产品负责人请在每个空白“评分（对／错／不确定）”单元格中填写一个判断。本文件未预填任何正确性判断；机器只标注调用失败、空答或格式不合。",
        "",
        "同一游戏下三个模型的答案按字段并排；知识模式与联网模式分表。",
    ]
    lookup = {
        (attempt.game_id, attempt.candidate_id, attempt.mode_id): attempt
        for attempt in run.attempts
    }
    for index, game in enumerate(GAMES, start=1):
        lines.extend(
            (
                "",
                f"## {index}. {game.game_name}（{game.display_name}）",
                "",
                f"入选理由：{game.category}；{game.era}；{game.reason}",
            )
        )
        for mode in MODES:
            lines.extend(
                (
                    "",
                    f"### {mode.label}",
                    "",
                    "| 字段 | "
                    + " | ".join(
                        value
                        for candidate in CANDIDATES
                        for value in (
                            f"{candidate.label}（{mode.label}）答案",
                            "评分（对／错／不确定）",
                        )
                    )
                    + " |",
                    "|---|" + "---|---|" * len(CANDIDATES),
                )
            )
            malformed: list[tuple[Candidate, Attempt]] = []
            for field, field_label in ANSWER_FIELDS:
                cells = [field_label]
                for candidate in CANDIDATES:
                    attempt = lookup[(game.game_id, candidate.candidate_id, mode.mode_id)]
                    cells.extend((_answer_cell(attempt, field), ""))
                    if attempt.format_error is not None and attempt.response_text.strip():
                        malformed.append((candidate, attempt))
                lines.append("| " + " | ".join(cells) + " |")
            if malformed:
                lines.extend(("", "格式不合原文（只供判卷，不作修复）：", ""))
                seen: set[str] = set()
                for candidate, attempt in malformed:
                    if candidate.candidate_id in seen:
                        continue
                    seen.add(candidate.candidate_id)
                    escaped = html.escape(attempt.response_text).replace("\n", "<br>")
                    lines.append(
                        f"- **{candidate.label}**：{html.escape(attempt.format_error or '')}；原文：{escaped}"
                    )
    lines.append("")
    return "\n".join(lines)


def render_prompt() -> str:
    return "\n".join(
        (
            "# M5-B-T3a 统一提示词",
            "",
            "三个模型、知识／联网两种模式使用以下 system prompt，逐字相同。游戏名不属于提示词规则，而是运行时数据；用户消息固定使用同一模板，仅替换 `{game_name}`。",
            "",
            "## System prompt（逐字全文）",
            "",
            "```text",
            SYSTEM_PROMPT,
            "```",
            "",
            "## 用户消息模板（逐字全文）",
            "",
            "```text",
            USER_PROMPT_TEMPLATE,
            "```",
            "",
            "## 固定调用参数",
            "",
            f"- temperature：`{TEMPERATURE}`",
            f"- max_tokens：`{MAX_TOKENS}`",
            f"- 客户端超时配置：`{TIMEOUT_SECONDS}` 秒",
            "- 知识模式：无工具。",
            "- 联网模式：仅增加网关内置 `openrouter:web_search` Server Tool；system prompt 和用户消息模板不变。",
            "",
        )
    )


def write_raw_results(output: Path, run: ProbeRun) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(
        json.dumps(
            {
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "attempts": [asdict(item) for item in run.attempts],
                "dispatch_stats": [asdict(item) for item in run.dispatch_stats],
                "deviations": list(run.deviations),
                "prior_unpersisted_call_count": run.prior_unpersisted_call_count,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def write_outputs(output: Path, run: ProbeRun) -> None:
    write_raw_results(output, run)
    # Persist the complete raw run before rendering derived Markdown. A report
    # bug must never make completed, paid probe calls unrecoverable.
    (output / "report.md").write_text(render_report(run), encoding="utf-8")
    (output / "judging.md").write_text(render_judging(run), encoding="utf-8")
    (output / "prompt.md").write_text(render_prompt(), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--seed-results",
        type=Path,
        help="reuse already-attempted logical cells without dispatching them again",
    )
    parser.add_argument(
        "--render-results",
        type=Path,
        help="render saved raw results without dispatching any model calls",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    started = time.perf_counter()
    if arguments.seed_results is not None and arguments.render_results is not None:
        raise ValueError("--seed-results and --render-results are mutually exclusive")
    if arguments.render_results is not None:
        run = load_run(arguments.render_results)
    else:
        seed = load_seed(arguments.seed_results) if arguments.seed_results else None
        run = run_probe(
            seed=seed,
            checkpoint=lambda partial: write_raw_results(arguments.output, partial),
        )
    write_outputs(arguments.output, run)
    print(
        json.dumps(
            {
                "attempts": len(run.attempts),
                "elapsed_seconds": time.perf_counter() - started,
                "output": str(arguments.output.resolve()),
                "cost_usd": sum(
                    attempt.cost_usd
                    for attempt in run.attempts
                    if attempt.cost_usd is not None
                ),
                "rate_limits": sum(
                    item.rate_limit_count for item in run.dispatch_stats
                ),
                "cooldown_drops": sum(
                    item.cooldown_drop_count for item in run.dispatch_stats
                ),
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
