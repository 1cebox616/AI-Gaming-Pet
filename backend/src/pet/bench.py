"""Offline evaluation of GSI-event-card-driven CS2 commentary."""

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
from pet.prompt import PROMPTS_DIRECTORY, PromptPersonality, load_system_prompt
from pet.replay import load_recording, replay_commentary
from pet.event_card import render_event_card

TEMPERATURE = 0.9
MAX_COMPLETION_TOKENS_BY_PERSONALITY: dict[PromptPersonality, int] = {
    "brother": 96,
    "caster": 96,
    "inference": 300,
}
TEMPLATE_PERSONALITY_BY_PROMPT: dict[PromptPersonality, PersonalityStyle] = {
    "brother": "brother",
    "caster": "caster",
    "inference": "brother",
}

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

    exceeds_max_chars: bool | None
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
    event_card: str
    template_text: str
    attempt: BenchAttempt | None


@dataclass(frozen=True, slots=True)
class BenchResult:
    """All data needed to render one self-contained Markdown report."""

    recording_path: Path
    requested_model: str | None
    requested_provider: str | None
    personality_style: PromptPersonality
    system_prompt: str | None
    include_reading_guide: bool
    cards_only: bool
    run_timestamp: datetime
    snapshot_count: int
    detected_event_count: int
    selected_event_count: int
    truncated_event_count: int
    length_statistics: LengthStatistics
    events: tuple[BenchEvent, ...]


@dataclass(frozen=True, slots=True)
class LatencyExperimentResult:
    """Sequential A/B/C runs that isolate prompt and output-length changes."""

    recording_path: Path
    requested_model: str
    requested_provider: str | None
    run_timestamp: datetime
    groups: tuple[BenchResult, BenchResult, BenchResult]


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
    """Measure all template lines for the selected production personality."""
    texts = tuple(
        template.text
        for templates in COMMENTARY_TEMPLATES[personality_style].values()
        for template in templates
    )
    return calculate_length_statistics(texts)


def check_output(
    text: str,
    *,
    max_chars: int,
    enforce_length_limit: bool = True,
) -> OutputChecks:
    """Record factual violations without filtering or regenerating the output."""
    return OutputChecks(
        exceeds_max_chars=(
            _chinese_character_count(text) > max_chars
            if enforce_length_limit
            else None
        ),
        callout_terms=tuple(term for term in CALLOUT_TERMS if term in text),
        raw_curses=tuple(term for term in FORBIDDEN_RAW_CURSES if term in text),
    )


def run_bench(
    recording_path: Path,
    *,
    model: str | None,
    provider: str | None,
    personality_style: PromptPersonality,
    client: LlmClientProtocol | None,
    max_events: int,
    prompts_directory: Path = PROMPTS_DIRECTORY,
    include_reading_guide: bool = True,
    cards_only: bool = False,
) -> BenchResult:
    """Replay one recording and optionally call the model for selected events."""
    if max_events < 1:
        raise ValueError("max_events must be at least 1")
    if not cards_only and (model is None or client is None):
        raise ValueError("非 cards-only 模式必须提供型号与模型客户端")

    template_personality = TEMPLATE_PERSONALITY_BY_PROMPT[personality_style]
    snapshots = load_recording(recording_path)
    configuration = load_config()
    replay = replay_commentary(
        snapshots,
        configuration.events,
        configuration.policy,
        personality_style=template_personality,
    )
    selected = tuple(
        disposition
        for disposition in replay.dispositions
        if disposition.decision.selected
    )
    chosen = selected[:max_events]
    length_statistics = commentary_length_statistics(template_personality)
    system_prompt = (
        None
        if cards_only
        else load_system_prompt(
            personality_style,
            max_chars=length_statistics.p90,
            prompts_directory=prompts_directory,
            include_reading_guide=include_reading_guide,
        )
    )
    events: list[BenchEvent] = []

    for disposition in chosen:
        event = disposition.decision.event
        if disposition.utterance is None:
            raise ValueError(f"selected event {event.id} has no template utterance")
        card = render_event_card(
            disposition.snapshot,
            disposition.game,
            disposition.round_situation,
            event,
        )
        attempt = None
        if not cards_only:
            assert model is not None
            assert client is not None
            assert system_prompt is not None
            attempt = _attempt_completion(
                model=model,
                provider=provider,
                system_prompt=system_prompt,
                event_card=card,
                max_chars=length_statistics.p90,
                max_tokens=MAX_COMPLETION_TOKENS_BY_PERSONALITY[personality_style],
                enforce_length_limit=personality_style != "inference",
                client=client,
            )
        events.append(
            BenchEvent(
                event=event,
                category=commentary_category(event),
                event_card=card,
                template_text=disposition.utterance.text,
                attempt=attempt,
            )
        )

    return BenchResult(
        recording_path=recording_path,
        requested_model=model,
        requested_provider=provider,
        personality_style=personality_style,
        system_prompt=system_prompt,
        include_reading_guide=include_reading_guide,
        cards_only=cards_only,
        run_timestamp=datetime.now(timezone.utc),
        snapshot_count=len(snapshots),
        detected_event_count=len(replay.dispositions),
        selected_event_count=len(selected),
        truncated_event_count=max(0, len(selected) - len(chosen)),
        length_statistics=length_statistics,
        events=tuple(events),
    )


def run_latency_experiment(
    recording_path: Path,
    *,
    model: str,
    provider: str | None,
    client: LlmClientProtocol,
    max_events: int,
    prompts_directory: Path = PROMPTS_DIRECTORY,
) -> LatencyExperimentResult:
    """Run A, then B, then C through one reusable client connection."""
    group_a = run_bench(
        recording_path,
        model=model,
        provider=provider,
        personality_style="brother",
        client=client,
        max_events=max_events,
        prompts_directory=prompts_directory,
        include_reading_guide=False,
    )
    group_b = run_bench(
        recording_path,
        model=model,
        provider=provider,
        personality_style="brother",
        client=client,
        max_events=max_events,
        prompts_directory=prompts_directory,
        include_reading_guide=True,
    )
    group_c = run_bench(
        recording_path,
        model=model,
        provider=provider,
        personality_style="inference",
        client=client,
        max_events=max_events,
        prompts_directory=prompts_directory,
        include_reading_guide=True,
    )
    return LatencyExperimentResult(
        recording_path=recording_path,
        requested_model=model,
        requested_provider=provider,
        run_timestamp=datetime.now(timezone.utc),
        groups=(group_a, group_b, group_c),
    )


def render_report(result: BenchResult) -> str:
    """Render full cards plus model outputs, or a zero-call cards-only report."""
    attempts = tuple(
        item.attempt for item in result.events if item.attempt is not None
    )
    successful_results = tuple(
        attempt.result for attempt in attempts if attempt.result is not None
    )
    actual_models = sorted({item.model for item in successful_results})
    actual_providers = sorted(
        {item.provider for item in successful_results if item.provider is not None}
    )
    stats = result.length_statistics
    provider_label = (
        f"`{result.requested_provider}`（已锁定且关闭自动回退）"
        if result.requested_provider is not None
        else "未锁定（延迟数字不可比）"
    )
    lines = [
        "# GSI 事件卡推断评测报告"
        if not result.cards_only
        else "# GSI 事件卡审阅报告（cards-only）",
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
        f"- 模式：{'只出卡，不调用模型' if result.cards_only else '模型推断'}",
        f"- 请求型号 ID：{f'`{result.requested_model}`' if result.requested_model else '未调用模型'}",
        f"- 锁定的上游服务商：{provider_label if not result.cards_only else '未调用模型'}",
        f"- 上游实际返回型号：{_join_or_unavailable(actual_models)}",
        f"- 运行时间：{result.run_timestamp.isoformat()}",
    ]
    if result.system_prompt is not None:
        prompt_summary = tuple(
            line for line in result.system_prompt.splitlines() if line.strip()
        )[:2]
        lines.extend(
            (
                f"- 读卡指南：{'已拼接' if result.include_reading_guide else '已跳过'}",
                "- 系统提示词摘要：",
                *(f"  - {line}" for line in prompt_summary),
            )
        )
    lines.extend(
        (
            "- 模板汉字数（去除占位符）："
            f"最小 {stats.minimum} / 中位 {_format_number(stats.median)} / "
            f"P90 {stats.p90} / 最大 {stats.maximum}",
            "",
            "## 逐条结果",
            "",
        )
    )
    if not result.events:
        lines.extend(("（没有策略入选事件。）", ""))
    for index, item in enumerate(result.events, 1):
        event = item.event
        round_label = (
            f"第 {event.round_number} 回合"
            if event.round_number is not None
            else "回合数未提供"
        )
        lines.extend(
            (
                f"### 事件 {index} —— {_EVENT_LABELS[event.type]}"
                f"（{_CATEGORY_LABELS[item.category]}），{round_label}",
                "",
                "喂进去的 GSI 事件卡：",
                "",
                *(f"    {line}" for line in item.event_card.splitlines()),
                "",
            )
        )
        if item.attempt is not None:
            lines.extend(
                (
                    f"模板句：{item.template_text}",
                    f"模型句：{_attempt_text(item.attempt)}",
                    "",
                    _attempt_checks(
                        item.attempt,
                        max_chars=stats.p90,
                        enforce_length_limit=result.personality_style != "inference",
                    ),
                    _attempt_metrics(item.attempt),
                    "",
                )
            )

    lines.extend(("## 数字汇总", ""))
    if result.cards_only:
        lines.extend(
            (
                "- 模型调用次数：0（cards-only）",
                f"- 原样输出 GSI 事件卡：{len(result.events)}",
            )
        )
    else:
        lines.extend(
            _summary_lines(
                result.events,
                actual_providers,
                enforce_length_limit=result.personality_style != "inference",
            )
        )
    return "\n".join(lines) + "\n"


def render_latency_report(result: LatencyExperimentResult) -> str:
    """Render the sequential A/B/C latency attribution experiment."""
    labels = (
        ("A", "仅 brother 性格文件；96 token 上限"),
        ("B", "reading.md + brother；96 token 上限"),
        ("C", "reading.md + inference；300 token 上限"),
    )
    provider_label = (
        f"`{result.requested_provider}`（已锁定且关闭自动回退）"
        if result.requested_provider is not None
        else "未锁定（延迟数字不可比）"
    )
    lines = [
        "# M3-T4 延迟归因实验",
        "",
        "## 实验设置",
        "",
        f"- 录制文件：`{result.recording_path}`",
        f"- 请求型号 ID：`{result.requested_model}`",
        f"- 上游服务商：{provider_label}",
        "- 执行顺序：A → B → C，串行、不并发，共用同一个 HTTP 客户端连接",
        "- 首次调用位于 A 组，会承担可能存在的一次性连接建立开销",
        f"- 运行时间：{result.run_timestamp.isoformat()}",
        "",
        "## 数字汇总",
        "",
        "| 组 | 配置 | 调用/成功/失败 | 输入 token 中位数 | 输出 token 中位数 | 延迟 P50 / P95 / 最大 | 单次花费中位数 |",
        "|---|---|---:|---:|---:|---|---:|",
    ]
    metrics = []
    for group, (label, description) in zip(result.groups, labels, strict=True):
        values = _latency_metrics(group.events)
        metrics.append(values)
        lines.append(
            f"| {label} | {description} | "
            f"{values.call_count}/{values.success_count}/{values.failure_count} | "
            f"{_optional_median(values.prompt_tokens)} | "
            f"{_optional_median(values.completion_tokens)} | "
            f"{_latency_values(values.latencies)} | "
            f"{_optional_cost_median(values.costs)} |"
        )
    lines.extend(("", "## 结论", "", _latency_conclusion(metrics)))
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class _LatencyMetrics:
    call_count: int
    success_count: int
    failure_count: int
    prompt_tokens: tuple[int, ...]
    completion_tokens: tuple[int, ...]
    latencies: tuple[float, ...]
    costs: tuple[float, ...]


def write_report(result: BenchResult, output_path: Path) -> None:
    """Write one benchmark report to the explicitly requested location."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_report(result), encoding="utf-8")


def write_latency_report(
    result: LatencyExperimentResult, output_path: Path
) -> None:
    """Write one A/B/C latency report to the explicitly requested location."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_latency_report(result), encoding="utf-8")


def _attempt_completion(
    *,
    model: str,
    provider: str | None,
    system_prompt: str,
    event_card: str,
    max_chars: int,
    max_tokens: int,
    enforce_length_limit: bool,
    client: LlmClientProtocol,
) -> BenchAttempt:
    try:
        result = client.complete(
            model=model,
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=event_card,
            max_tokens=max_tokens,
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
        checks=check_output(
            result.text,
            max_chars=max_chars,
            enforce_length_limit=enforce_length_limit,
        ),
    )


def _chinese_character_count(text: str) -> int:
    return len(_HAN_CHARACTER_PATTERN.findall(_remove_placeholders(text)))


def _remove_placeholders(text: str) -> str:
    return _PLACEHOLDER_PATTERN.sub("", text).strip()


def _attempt_text(attempt: BenchAttempt) -> str:
    if attempt.result is not None:
        return attempt.result.text
    return f"调用失败：{attempt.error}"


def _attempt_checks(
    attempt: BenchAttempt,
    *,
    max_chars: int,
    enforce_length_limit: bool,
) -> str:
    if attempt.result is None or attempt.checks is None:
        return "自动检查：调用失败，未检查"
    checks = attempt.checks
    count = _chinese_character_count(attempt.result.text)
    length_label = (
        f"字数 {count}/{max_chars} "
        f"{'✓' if checks.exceeds_max_chars is False else '✗'}"
        if enforce_length_limit
        else f"字数 {count}（本轮不限字数）"
    )
    return (
        f"自动检查：{length_label} ｜ "
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
    *,
    enforce_length_limit: bool,
) -> list[str]:
    attempts = tuple(
        event.attempt for event in events if event.attempt is not None
    )
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
    latency_label = _latency_values(latencies)
    cost_label = (
        f"${sum(costs):.6f}"
        if successes and len(costs) == len(successes)
        else "上游未完整提供"
    )
    length_summary = (
        f"{sum(item.exceeds_max_chars is True for item in checks)}"
        if enforce_length_limit
        else "本轮不限字数"
    )
    return [
        f"- 调用次数：{len(attempts)}",
        f"- 成功：{len(successes)}；失败：{len(attempts) - len(successes)}",
        f"- 延迟：{latency_label}",
        f"- 输入 token：{_token_summary(prompt_tokens, len(successes))}",
        f"- 输出 token：{_token_summary(completion_tokens, len(successes))}",
        f"- 总花费：{cost_label}",
        f"- 超出字数上限：{length_summary}",
        f"- 命中点位词：{sum(bool(item.callout_terms) for item in checks)}",
        f"- 命中未替代脏字：{sum(bool(item.raw_curses) for item in checks)}",
        f"- 实际返回的上游服务商去重列表：{_join_or_unavailable(actual_providers)}",
    ]


def _latency_metrics(events: Sequence[BenchEvent]) -> _LatencyMetrics:
    attempts = tuple(
        event.attempt for event in events if event.attempt is not None
    )
    results = tuple(
        attempt.result for attempt in attempts if attempt.result is not None
    )
    return _LatencyMetrics(
        call_count=len(attempts),
        success_count=len(results),
        failure_count=len(attempts) - len(results),
        prompt_tokens=tuple(
            item.usage.prompt_tokens
            for item in results
            if item.usage.prompt_tokens is not None
        ),
        completion_tokens=tuple(
            item.usage.completion_tokens
            for item in results
            if item.usage.completion_tokens is not None
        ),
        latencies=tuple(item.latency_seconds for item in results),
        costs=tuple(
            item.usage.cost_usd for item in results if item.usage.cost_usd is not None
        ),
    )


def _latency_conclusion(metrics: Sequence[_LatencyMetrics]) -> str:
    if len(metrics) != 3 or any(not item.latencies for item in metrics):
        return "有分组缺少成功调用，无法归因延迟增量。"
    p50_a, p50_b, p50_c = (
        _percentile(item.latencies, 0.50) for item in metrics
    )
    prompt_delta = p50_b - p50_a
    output_delta = p50_c - p50_b
    if abs(prompt_delta) > abs(output_delta):
        source = "输入提示词变长"
    elif abs(output_delta) > abs(prompt_delta):
        source = "输出变长"
    else:
        source = "两者相当"
    return (
        f"P50 延迟 A→B {_signed_seconds(prompt_delta)}，"
        f"B→C {_signed_seconds(output_delta)}；"
        f"本次观察到的主要增量来自{source}。"
    )


def _latency_values(values: Sequence[float]) -> str:
    if not values:
        return "无成功调用"
    return (
        f"P50 {_percentile(values, 0.50):.3f}s / "
        f"P95 {_percentile(values, 0.95):.3f}s / 最大 {max(values):.3f}s"
    )


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * quantile) - 1]


def _token_summary(values: Sequence[int], success_count: int) -> str:
    if not values or len(values) != success_count:
        return "上游未完整提供"
    return f"总计 {sum(values)} / 平均 {statistics.fmean(values):.1f}"


def _optional_median(values: Sequence[int]) -> str:
    return f"{statistics.median(values):g}" if values else "上游未提供"


def _optional_cost_median(values: Sequence[float]) -> str:
    return f"${statistics.median(values):.6f}" if values else "上游未提供"


def _signed_seconds(value: float) -> str:
    return f"增加 {value:.3f}s" if value >= 0 else f"减少 {abs(value):.3f}s"


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
    parser = argparse.ArgumentParser(description="离线评测 CS2 GSI 事件卡驱动的模型推断")
    parser.add_argument("--replay", type=Path, required=True, help="GSI JSONL 录制文件")
    parser.add_argument("--model", help="OpenRouter 型号 ID；cards-only 时可省略")
    parser.add_argument("--provider", help="锁定的 OpenRouter 上游服务商 slug")
    parser.add_argument("--out", type=Path, required=True, help="Markdown 报告输出路径")
    parser.add_argument(
        "--personality",
        choices=("brother", "caster", "inference"),
        default="brother",
        help="要评测的提示词性格",
    )
    parser.add_argument(
        "--max-events",
        type=_positive_int,
        default=40,
        help="最多评测多少个策略入选事件",
    )
    parser.add_argument(
        "--cards-only",
        action="store_true",
        help="只渲染 GSI 事件卡，不读取密钥或调用模型",
    )
    parser.add_argument(
        "--no-reading-guide",
        action="store_true",
        help="评测时跳过共享 reading.md；只用于对照实验",
    )
    parser.add_argument(
        "--latency-experiment",
        action="store_true",
        help="按 A→B→C 串行运行延迟归因实验",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run offline evaluation without exposing credentials in arguments."""
    args = _build_parser().parse_args(argv)
    if args.cards_only and args.latency_experiment:
        print("错误：--cards-only 与 --latency-experiment 不能同时使用", file=sys.stderr)
        return 2
    if args.no_reading_guide and (args.cards_only or args.latency_experiment):
        print("错误：该模式不能额外指定 --no-reading-guide", file=sys.stderr)
        return 2
    if not args.cards_only and args.model is None:
        print("错误：模型评测必须传入 --model", file=sys.stderr)
        return 2

    if args.cards_only:
        result = run_bench(
            args.replay,
            model=None,
            provider=None,
            personality_style=args.personality,
            client=None,
            max_events=args.max_events,
            cards_only=True,
        )
        write_report(result, args.out)
        print(f"GSI 事件卡报告已写入：{args.out}")
        return 0

    try:
        client = OpenRouterClient.from_env()
    except LlmError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    try:
        assert args.model is not None
        if args.latency_experiment:
            experiment = run_latency_experiment(
                args.replay,
                model=args.model,
                provider=args.provider,
                client=client,
                max_events=args.max_events,
            )
            write_latency_report(experiment, args.out)
        else:
            result = run_bench(
                args.replay,
                model=args.model,
                provider=args.provider,
                personality_style=args.personality,
                client=client,
                max_events=args.max_events,
                include_reading_guide=not args.no_reading_guide,
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
