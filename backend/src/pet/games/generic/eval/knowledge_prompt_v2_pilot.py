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
import statistics
from typing import Protocol

from pet.core.llm import LlmDispatchStats, LlmError, LlmResult
from pet.games.generic.eval.knowledge_model_probe import (
    BACKEND_DIRECTORY,
    GAMES,
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
TEMPERATURE = 0.0
MAX_TOKENS = 2400
TIMEOUT_SECONDS = 45.0
LATENCY_TARGET_SECONDS = 10.0
PILOT_GAME_IDS = (
    "overwatch-2",
    "dont-starve-together",
    "slay-the-spire-2",
)


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
  "default_pc_keybinds": [
    {"action": "核心动作", "input": "PC 默认键盘鼠标输入"}
  ]
}

详细度要求：
- game_overview 应为信息密度高的完整段落，不是一句话宣传语。
- gameplay.core_loop 应覆盖开始、进行、反馈与继续循环；major_systems 写 4 至 10 个真正影响玩法的系统。
- background 只提供理解游戏所需的公开前提，不复述故事。
- default_pc_keybinds 写 6 至 15 个最常用核心动作；只写 PC 默认键盘鼠标。若该游戏没有统一可靠的 PC 默认绑定，对相应 input 写“不确定”，不要猜测。
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
    ) -> LlmResult: ...

    def dispatch_stats(self) -> LlmDispatchStats: ...

    def close(self) -> None: ...


ClientFactory = Callable[[ProbeMode], PilotClient]


def pilot_games() -> tuple[GameCase, ...]:
    by_id = {game.game_id: game for game in GAMES}
    return tuple(by_id[game_id] for game_id in PILOT_GAME_IDS)


def render_user_prompt(game_name: str) -> str:
    return USER_PROMPT_TEMPLATE.format(game_name=game_name)


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def parse_answer(text: str) -> tuple[dict[str, object] | None, str | None]:
    if not text.strip():
        return None, "空答"
    try:
        value: object = json.loads(text)
    except json.JSONDecodeError as error:
        return None, (
            f"不是合法 JSON：{error.msg}（line {error.lineno}, column {error.colno}）"
        )
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
    if not isinstance(keybinds, list) or not 6 <= len(keybinds) <= 15:
        return None, "default_pc_keybinds 必须含 6 至 15 项"
    for index, item in enumerate(keybinds):
        if (
            not isinstance(item, dict)
            or set(item) != {"action", "input"}
            or not all(_nonempty_string(item.get(key)) for key in item)
        ):
            return None, f"default_pc_keybinds[{index}] 字段不匹配或为空"
    return value, None


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
                    )
                    parsed, format_error = parse_answer(result.text)
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
    if attempt.format_error is not None:
        return "格式不合"
    return "成功"


def _cell(value: object) -> str:
    return html.escape(str(value)).replace("|", "&#124;").replace("\n", "<br>")


def render_report(run: PilotRun) -> str:
    lines = [
        "# M5-B-T3a DeepSeek V4 提示词 V2 小样本报告",
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
        f"- 固定参数：temperature={TEMPERATURE}，max_tokens={MAX_TOKENS}，客户端超时={TIMEOUT_SECONDS:.0f} 秒；产品延迟目标仍按 ≤{LATENCY_TARGET_SECONDS:.0f} 秒统计。",
        "- 这里的 httpx 超时是连接／读取等 I/O 阶段的超时，不是整次请求的总墙钟截止；联网端点持续传输数据时，实测总耗时可以明显超过 45 秒。",
        "",
        "## 提示词调整",
        "",
        "- 删除社区术语与 HUD 惯例。",
        "- 将一句话核心玩法扩成完整游戏介绍、详细玩法结构与不剧透的公开背景。",
        "- 默认键位统一为 PC 键盘鼠标，只保留 action / input。",
        "- 输出面向后续每个视觉模型的稳定 context，因此完整性优先于极端短小。",
        "",
        "## 耗时与花费",
        "",
        "| 模式 | 返回 | 格式合规 | P50 / P90 / 最大（秒） | ≤10 秒 | 花费（USD） |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for mode in PILOT_MODES:
        attempts = [item for item in run.attempts if item.mode_id == mode.mode_id]
        returned = [item for item in attempts if item.error is None and item.response_text.strip()]
        latencies = [item.latency_seconds for item in attempts if item.latency_seconds is not None]
        valid = sum(item.parsed_answer is not None for item in attempts)
        target = sum(
            item.latency_seconds is not None
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
            "- 这是 3 个游戏的小样本探针，不能替代正式跨类型判卷。",
            "- 答案未由脚本判定事实正确性；完整原文见 answers.md。",
            "- 详细输出与 10 秒延迟目标存在客观张力，本表保留实测，不据此自动调短提示词。",
            "- 预检记录：受限沙箱首次在建连前阻断，未触达网关；获准联网后，锁定 DeepSeek 自营上游的 3 个请求因账户数据策略返回 HTTP 404，零计费且模型未执行。随后改为 OpenRouter 合规上游自动路由，得到本报告中的 3 个有效调用。",
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
        "# DeepSeek V4 提示词 V2 原始答案",
        "",
        "以下内容按调用原样保存；不预填事实正确性判断。",
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
                lines.extend(("```json", attempt.response_text, "```"))
            lines.append("")
    return "\n".join(lines)


def render_prompt() -> str:
    return "\n".join(
        (
            "# M5-B-T3a 游戏知识线提示词 V2",
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
            f"- 客户端超时：`{TIMEOUT_SECONDS}` 秒",
            "- 联网模式：只增加网关内置 `openrouter:web_search` Server Tool。",
            "",
        )
    )


def write_raw_results(output: Path, run: PilotRun) -> None:
    output.mkdir(parents=True, exist_ok=True)
    payload = {
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "model": MODEL,
        "provider": PROVIDER,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "latency_target_seconds": LATENCY_TARGET_SECONDS,
        "pilot_game_ids": list(PILOT_GAME_IDS),
        "attempts": [asdict(item) for item in run.attempts],
        "dispatch_stats": [asdict(item) for item in run.dispatch_stats],
    }
    (output / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_outputs(output: Path, run: PilotRun) -> None:
    write_raw_results(output, run)
    (output / "report.md").write_text(render_report(run), encoding="utf-8")
    (output / "answers.md").write_text(render_answers(run), encoding="utf-8")
    (output / "prompt-v2.md").write_text(render_prompt(), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    run = run_pilot(checkpoint_output=arguments.output)
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
