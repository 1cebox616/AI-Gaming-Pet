"""Offline evaluation of situation-card-driven CS2 commentary."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path
import re
import statistics
import sys

from pet.commentary import commentary_category
from pet.commentary_rules import CALLOUT_TERMS, FORBIDDEN_RAW_CURSES
from pet.commentary_templates import COMMENTARY_TEMPLATES, CommentaryCategory
from pet.config import PersonalityStyle, load_config
from pet.events import EventType, GameEvent
from pet.llm import LlmClientProtocol, LlmError, LlmResult, OpenRouterClient
from pet.prompt import PROMPTS_DIRECTORY, load_system_prompt
from pet.replay import load_recording, replay_commentary
from pet.situation_card import render_situation_card

TEMPERATURE = 0.9
MAX_COMPLETION_TOKENS = 96

_PLACEHOLDER_PATTERN = re.compile(r"\{[^{}]+\}")
_HAN_CHARACTER_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

_EVENT_LABELS: dict[EventType, str] = {
    "kill": "普通击杀",
    "kill_headshot": "爆头击杀",
    "multi_kill": "多杀",
    "death": "死亡",
    "death_after_kill": "击杀后被补枪",
    "death_thrown_away": "死亡（白给）",
    "round_win": "回合胜利",
    "round_loss": "回合失败",
}
_CATEGORY_LABELS: dict[CommentaryCategory, str] = {
    "kill": "普通击杀",
    "kill_headshot": "爆头击杀",
    "multi_2": "双杀",
    "multi_3": "三杀",
    "multi_4": "四杀",
    "multi_5": "五杀",
    "multi_general": "多杀",
    "death": "普通死亡",
    "death_after_kill": "击杀后被补枪",
    "death_thrown_away": "白给",
    "round_win_elimination": "回合胜利·灭队",
    "round_win_bomb": "回合胜利·引爆",
    "round_win_defuse": "回合胜利·拆包",
    "round_win_time": "回合胜利·时间到",
    "round_win_general": "回合胜利·通用",
    "round_loss_elimination": "回合失败·灭队",
    "round_loss_bomb": "回合失败·引爆",
    "round_loss_defuse": "回合失败·拆包",
    "round_loss_time": "回合失败·时间到",
    "round_loss_general": "回合失败·通用",
}


@dataclass(frozen=True, slots=True)
class LengthStatistics:
    """Chinese-character distribution of one personality's template pool."""

    minimum: int
    median: float
    p90: int
    maximum: int


@dataclass(frozen=True, slots=True)
class OutputChecks:
    """Factual violations observed in one successful model output."""

    exceeds_max_chars: bool
    callout_terms: tuple[str, ...]
    raw_curses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BenchAttempt:
    """One model call's success or failure."""

    result: LlmResult | None
    error: str | None
    checks: OutputChecks | None


@dataclass(frozen=True, slots=True)
class BenchEvent:
    """One selected production event and the facts shown to the model."""

    event: GameEvent
    category: CommentaryCategory
    situation_card: str
    template_text: str
    attempt: BenchAttempt


@dataclass(frozen=True, slots=True)
class BenchResult:
    """All data needed to render one self-contained Markdown report."""

    recording_path: Path
    requested_model: str
    requested_provider: str | None
    personality_style: PersonalityStyle
    system_prompt: str
    run_timestamp: datetime
    snapshot_count: int
    detected_event_count: int
    selected_event_count: int
    truncated_event_count: int
    length_statistics: LengthStatistics
    events: tuple[BenchEvent, ...]


def calculate_length_statistics(texts: Sequence[str]) -> LengthStatistics:
    """Calculate nearest-rank P90 after removing format placeholders."""
    lengths = sorted(_chinese_character_count(text) for text in texts)
    if not lengths:
        raise ValueError("cannot calculate template lengths from an empty sequence")
    p90_index = math.ceil(len(lengths) * 0.9) - 1
    return LengthStatistics(
        minimum=lengths[0],
        median=float(statistics.median(lengths)),
        p90=lengths[p90_index],
        maximum=lengths[-1],
    )


def commentary_length_statistics(
    personality_style: PersonalityStyle,
) -> LengthStatistics:
    """Measure all template lines for the selected personality."""
    texts = tuple(
        template.text
        for templates in COMMENTARY_TEMPLATES[personality_style].values()
        for template in templates
    )
    return calculate_length_statistics(texts)


def check_output(text: str, *, max_chars: int) -> OutputChecks:
    """Record factual violations without filtering or regenerating the output."""
    return OutputChecks(
        exceeds_max_chars=_chinese_character_count(text) > max_chars,
        callout_terms=tuple(term for term in CALLOUT_TERMS if term in text),
        raw_curses=tuple(term for term in FORBIDDEN_RAW_CURSES if term in text),
    )


def run_bench(
    recording_path: Path,
    *,
    model: str,
    provider: str | None,
    personality_style: PersonalityStyle,
    client: LlmClientProtocol,
    max_events: int,
    prompts_directory: Path = PROMPTS_DIRECTORY,
) -> BenchResult:
    """Replay one recording and generate one model line per selected event."""
    if max_events < 1:
        raise ValueError("max_events must be at least 1")
    snapshots = load_recording(recording_path)
    configuration = load_config()
    replay = replay_commentary(
        snapshots,
        configuration.events,
        configuration.policy,
        personality_style=personality_style,
    )
    selected = tuple(
        disposition
        for disposition in replay.dispositions
        if disposition.decision.selected
    )
    chosen = selected[:max_events]
    length_statistics = commentary_length_statistics(personality_style)
    system_prompt = load_system_prompt(
        personality_style,
        max_chars=length_statistics.p90,
        prompts_directory=prompts_directory,
    )
    events: list[BenchEvent] = []

    for disposition in chosen:
        event = disposition.decision.event
        if disposition.utterance is None:
            raise ValueError(f"selected event {event.id} has no template utterance")
        card = render_situation_card(
            disposition.snapshot,
            disposition.game,
            disposition.round_situation,
            event,
        )
        events.append(
            BenchEvent(
                event=event,
                category=commentary_category(event),
                situation_card=card,
                template_text=disposition.utterance.text,
                attempt=_attempt_completion(
                    model=model,
                    provider=provider,
                    system_prompt=system_prompt,
                    situation_card=card,
                    max_chars=length_statistics.p90,
                    client=client,
                ),
            )
        )

    return BenchResult(
        recording_path=recording_path,
        requested_model=model,
        requested_provider=provider,
        personality_style=personality_style,
        system_prompt=system_prompt,
        run_timestamp=datetime.now(timezone.utc),
        snapshot_count=len(snapshots),
        detected_event_count=len(replay.dispositions),
        selected_event_count=len(selected),
        truncated_event_count=max(0, len(selected) - len(chosen)),
        length_statistics=length_statistics,
        events=tuple(events),
    )


def render_report(result: BenchResult) -> str:
    """Render the prompt, full cards, outputs, and aggregate metrics."""
    successful_results = tuple(
        item.attempt.result
        for item in result.events
        if item.attempt.result is not None
    )
    actual_models = sorted({item.model for item in successful_results})
    actual_providers = sorted(
        {item.provider for item in successful_results if item.provider is not None}
    )
    stats = result.length_statistics
    prompt_summary = tuple(
        line for line in result.system_prompt.splitlines() if line.strip()
    )[:2]
    provider_label = (
        f"`{result.requested_provider}`（已锁定且关闭自动回退）"
        if result.requested_provider is not None
        else "未锁定（延迟数字不可比）"
    )
    lines = [
        "# M3-T3 富卡话术评测报告",
        "",
        "## 本次运行",
        "",
        f"- 录制文件：`{result.recording_path}`",
        f"- 处理快照：{result.snapshot_count}",
        f"- 检测到事件：{result.detected_event_count}",
        f"- 策略入选事件：{result.selected_event_count}",
        f"- 本次实际评测：{len(result.events)}",
        f"- 因 `max_events` 截断：{result.truncated_event_count}",
        f"- 性格：`{result.personality_style}`",
        f"- 请求型号 ID：`{result.requested_model}`",
        f"- 锁定的上游服务商：{provider_label}",
        f"- 上游实际返回型号：{_join_or_unavailable(actual_models)}",
        f"- 运行时间：{result.run_timestamp.isoformat()}",
        "- 系统提示词摘要：",
        *(f"  - {line}" for line in prompt_summary),
        (
            "- 模板汉字数（去除占位符）："
            f"最小 {stats.minimum} / 中位 {_format_number(stats.median)} / "
            f"P90 {stats.p90} / 最大 {stats.maximum}"
        ),
        "",
        "## 逐条结果",
        "",
    ]
    if not result.events:
        lines.extend(("（没有策略入选事件。）", ""))
    for index, item in enumerate(result.events, 1):
        event = item.event
        round_label = (
            f"第 {event.round_number} 回合"
            if event.round_number is not None
            else "回合数未提供"
        )
        event_label = _EVENT_LABELS[event.type]
        category_label = _CATEGORY_LABELS[item.category]
        lines.extend(
            (
                f"### 事件 {index} —— {event_label}（{category_label}），{round_label}",
                "",
                "喂进去的富卡：",
                "",
                *(f"    {line}" for line in item.situation_card.splitlines()),
                "",
                f"模板句：{item.template_text}",
                f"模型句：{_attempt_text(item.attempt)}",
                "",
                _attempt_checks(item.attempt, max_chars=stats.p90),
                _attempt_metrics(item.attempt),
                "",
            )
        )

    lines.extend(("## 数字汇总", ""))
    lines.extend(_summary_lines(result.events, actual_providers))
    return "\n".join(lines) + "\n"


def write_report(result: BenchResult, output_path: Path) -> None:
    """Write the rendered report to the explicitly requested location."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_report(result), encoding="utf-8")


def _attempt_completion(
    *,
    model: str,
    provider: str | None,
    system_prompt: str,
    situation_card: str,
    max_chars: int,
    client: LlmClientProtocol,
) -> BenchAttempt:
    try:
        result = client.complete(
            model=model,
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=situation_card,
            max_tokens=MAX_COMPLETION_TOKENS,
            temperature=TEMPERATURE,
        )
    except LlmError as error:
        context = []
        if error.status_code is not None:
            context.append(f"HTTP {error.status_code}")
        if error.latency_seconds is not None:
            context.append(f"{error.latency_seconds:.3f}s")
        suffix = f"（{'，'.join(context)}）" if context else ""
        return BenchAttempt(result=None, error=f"{error}{suffix}", checks=None)
    return BenchAttempt(
        result=result,
        error=None,
        checks=check_output(result.text, max_chars=max_chars),
    )


def _chinese_character_count(text: str) -> int:
    return len(_HAN_CHARACTER_PATTERN.findall(_remove_placeholders(text)))


def _remove_placeholders(text: str) -> str:
    return _PLACEHOLDER_PATTERN.sub("", text).strip()


def _attempt_text(attempt: BenchAttempt) -> str:
    if attempt.result is not None:
        return attempt.result.text
    return f"调用失败：{attempt.error}"


def _attempt_checks(attempt: BenchAttempt, *, max_chars: int) -> str:
    if attempt.result is None or attempt.checks is None:
        return "自动检查：调用失败，未检查"
    checks = attempt.checks
    count = _chinese_character_count(attempt.result.text)
    return (
        f"自动检查：字数 {count}/{max_chars} "
        f"{'✓' if not checks.exceeds_max_chars else '✗'} ｜ "
        f"点位 {'✓' if not checks.callout_terms else '✗ ' + '、'.join(checks.callout_terms)} ｜ "
        f"脏字 {'✓' if not checks.raw_curses else '✗ ' + '、'.join(checks.raw_curses)}"
    )


def _attempt_metrics(attempt: BenchAttempt) -> str:
    if attempt.result is None:
        return "延迟/Token/上游：调用失败"
    result = attempt.result
    return (
        f"延迟 {result.latency_seconds:.3f}s ｜ "
        f"输入 {_optional_number(result.usage.prompt_tokens)} token ｜ "
        f"输出 {_optional_number(result.usage.completion_tokens)} token ｜ "
        f"花费 {_cost_label(result.usage.cost_usd)} ｜ "
        f"上游 {result.provider or '上游未提供'}"
    )


def _summary_lines(
    events: Sequence[BenchEvent],
    actual_providers: Sequence[str],
) -> list[str]:
    attempts = tuple(event.attempt for event in events)
    successes = tuple(attempt for attempt in attempts if attempt.result is not None)
    latencies = tuple(
        attempt.result.latency_seconds
        for attempt in successes
        if attempt.result is not None
    )
    prompt_tokens = tuple(
        attempt.result.usage.prompt_tokens
        for attempt in successes
        if attempt.result is not None and attempt.result.usage.prompt_tokens is not None
    )
    completion_tokens = tuple(
        attempt.result.usage.completion_tokens
        for attempt in successes
        if attempt.result is not None
        and attempt.result.usage.completion_tokens is not None
    )
    costs = tuple(
        attempt.result.usage.cost_usd
        for attempt in successes
        if attempt.result is not None and attempt.result.usage.cost_usd is not None
    )
    checks = tuple(attempt.checks for attempt in successes if attempt.checks is not None)
    latency_label = (
        f"P50 {_percentile(latencies, 0.50):.3f}s / "
        f"P95 {_percentile(latencies, 0.95):.3f}s / 最大 {max(latencies):.3f}s"
        if latencies
        else "无成功调用"
    )
    cost_label = (
        f"${sum(costs):.6f}"
        if successes and len(costs) == len(successes)
        else "上游未完整提供"
    )
    return [
        f"- 调用次数：{len(attempts)}",
        f"- 成功：{len(successes)}；失败：{len(attempts) - len(successes)}",
        f"- 延迟：{latency_label}",
        f"- 输入 token：{_token_summary(prompt_tokens, len(successes))}",
        f"- 输出 token：{_token_summary(completion_tokens, len(successes))}",
        f"- 总花费：{cost_label}",
        f"- 超出字数上限：{sum(item.exceeds_max_chars for item in checks)}",
        f"- 命中点位词：{sum(bool(item.callout_terms) for item in checks)}",
        f"- 命中未替代脏字：{sum(bool(item.raw_curses) for item in checks)}",
        f"- 实际返回的上游服务商去重列表：{_join_or_unavailable(actual_providers)}",
    ]


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * quantile) - 1]


def _token_summary(values: Sequence[int], success_count: int) -> str:
    if not values or len(values) != success_count:
        return "上游未完整提供"
    return f"总计 {sum(values)} / 平均 {statistics.fmean(values):.1f}"


def _format_number(value: float) -> str:
    return f"{value:g}"


def _optional_number(value: int | None) -> str:
    return str(value) if value is not None else "上游未提供"


def _cost_label(value: float | None) -> str:
    return f"${value:.6f}" if value is not None else "上游未提供"


def _join_or_unavailable(values: Sequence[str]) -> str:
    return "、".join(f"`{value}`" for value in values) if values else "上游未提供"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须至少为 1")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线评测 CS2 富卡驱动的模型话术")
    parser.add_argument("--replay", type=Path, required=True, help="GSI JSONL 录制文件")
    parser.add_argument("--model", required=True, help="OpenRouter 型号 ID（无默认值）")
    parser.add_argument("--provider", help="锁定的 OpenRouter 上游服务商 slug")
    parser.add_argument("--out", type=Path, required=True, help="Markdown 报告输出路径")
    parser.add_argument(
        "--personality",
        choices=("brother", "caster"),
        default="brother",
        help="要评测的现有性格",
    )
    parser.add_argument(
        "--max-events",
        type=_positive_int,
        default=40,
        help="最多评测多少个策略入选事件",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the offline benchmark without exposing credentials in arguments."""
    args = _build_parser().parse_args(argv)
    try:
        client = OpenRouterClient.from_env()
    except LlmError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    try:
        result = run_bench(
            args.replay,
            model=args.model,
            provider=args.provider,
            personality_style=args.personality,
            client=client,
            max_events=args.max_events,
        )
        write_report(result, args.out)
    except (FileNotFoundError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    finally:
        client.close()
    print(f"评测报告已写入：{args.out}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
