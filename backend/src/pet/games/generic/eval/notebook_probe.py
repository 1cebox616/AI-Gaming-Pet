"""Offline rolling-window probe for the generic observation notebook contract."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tomllib

from pet.core.config import LlmConfig, load_config, resolve_llm_profile
from pet.core.llm import LlmClientProtocol, LlmError, LlmResult, OpenRouterClient


BACKEND_DIRECTORY = Path(__file__).resolve().parents[5]
DEFAULT_PROMPT_PATH = BACKEND_DIRECTORY / "prompts" / "generic" / "notebook-probe.md"
DEFAULT_OUTPUT_ROOT = BACKEND_DIRECTORY / "eval-reports"
DEFAULT_WINDOW_SIZE = 60
DEFAULT_COST_CAP_USD = 1.0
SECTION_NAMES = ("稳定认知", "本轮变动", "未决问题", "信息缺口")
EMPTY_NOTEBOOK = """【稳定认知】
暂无

【本轮变动】
新增：暂无
驳回：暂无
翻案：暂无

【未决问题】
暂无

【信息缺口】
暂无"""
SECTION_PATTERN = re.compile(r"【(稳定认知|本轮变动|未决问题|信息缺口)】")
STABLE_ITEM_PATTERN = re.compile(r"^\s*\d+[.、]\s*")
LIST_PREFIX_PATTERN = re.compile(r"^\s*(?:[-*•]\s*|\d+[.、)]\s*)")
CHANGE_HEADING_PATTERN = re.compile(r"^\s*(新增|驳回|翻案)\s*[：:]\s*(.*)$")


class NotebookProbeError(Exception):
    """An operator-facing probe configuration or input error."""


@dataclass(frozen=True, slots=True)
class ObservationEntry:
    seq: int
    raw: str


@dataclass(frozen=True, slots=True)
class PreparedLog:
    path: Path
    name: str
    game: str
    entries: tuple[ObservationEntry, ...]
    sha256: str


@dataclass(frozen=True, slots=True)
class ParsedNotebook:
    text: str
    sections: dict[str, str]


@dataclass(slots=True)
class CostBudget:
    cap_usd: float
    guard_spend_usd: float = 0.0
    known_actual_usd: float = 0.0
    unknown_cost_windows: int = 0

    def can_start(self, estimated_call_cost: float) -> bool:
        return self.guard_spend_usd + estimated_call_cost <= self.cap_usd

    def charge(self, cost_usd: float | None, estimated_call_cost: float) -> None:
        if cost_usd is None:
            self.guard_spend_usd += estimated_call_cost
            self.unknown_cost_windows += 1
            return
        self.guard_spend_usd += cost_usd
        self.known_actual_usd += cost_usd


@dataclass(frozen=True, slots=True)
class CombinationSummary:
    log_name: str
    profile: str
    output_directory: Path
    total_cost_usd: float
    total_latency_ms: float
    final_cognition_count: int
    confirmed_count: int
    rejected_count: int
    information_gaps: tuple[str, ...]
    parse_failure_count: int
    api_failure_count: int
    stopped_by_cost_guard: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M5-B-T1 滚动窗口笔记本探针")
    parser.add_argument("--log", action="append", required=True, type=Path)
    parser.add_argument("--profile", action="append", required=True)
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_SIZE)
    parser.add_argument("--cost-cap", type=float, default=DEFAULT_COST_CAP_USD)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT_PATH)
    return parser


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return slug or "item"


def load_observation_log(path: Path) -> PreparedLog:
    resolved = path.resolve()
    if not resolved.is_file():
        raise NotebookProbeError(f"观察日志不存在：{resolved}")
    entries: list[ObservationEntry] = []
    games: set[str] = set()
    for line_number, raw in enumerate(
        resolved.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise NotebookProbeError(
                f"{resolved}:{line_number} 不是合法 JSON：{error}"
            ) from error
        if not isinstance(payload, dict):
            raise NotebookProbeError(f"{resolved}:{line_number} 顶层不是对象")
        seq = payload.get("seq")
        game = payload.get("game")
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise NotebookProbeError(f"{resolved}:{line_number} 缺少整数 seq")
        if not isinstance(game, str) or not game.strip():
            raise NotebookProbeError(f"{resolved}:{line_number} 缺少 game")
        entries.append(ObservationEntry(seq, raw))
        games.add(game.strip())
    if not entries:
        raise NotebookProbeError(f"观察日志为空：{resolved}")
    if len(games) != 1:
        raise NotebookProbeError(f"观察日志包含多个游戏名：{sorted(games)}")
    sequences = [entry.seq for entry in entries]
    if len(sequences) != len(set(sequences)):
        raise NotebookProbeError(f"观察日志含重复 seq：{resolved}")
    entries.sort(key=lambda entry: entry.seq)
    return PreparedLog(
        path=resolved,
        name=_slug(resolved.parent.name),
        game=next(iter(games)),
        entries=tuple(entries),
        sha256=_file_sha256(resolved),
    )


def split_windows(
    entries: Sequence[ObservationEntry], window_size: int
) -> tuple[tuple[ObservationEntry, ...], ...]:
    if window_size < 1:
        raise ValueError("window_size must be positive")
    return tuple(
        tuple(entries[index : index + window_size])
        for index in range(0, len(entries), window_size)
    )


def build_user_message(
    game: str,
    notebook: str,
    entries: Sequence[ObservationEntry],
) -> str:
    raw_entries = "\n".join(entry.raw for entry in entries)
    return (
        f"游戏名：\n{game}\n\n"
        f"当前笔记本全文：\n{notebook}\n\n"
        f"本窗观察条目原文：\n{raw_entries}"
    )


def parse_notebook(text: str) -> ParsedNotebook:
    candidate = text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()
    matches = list(SECTION_PATTERN.finditer(candidate))
    names = tuple(match.group(1) for match in matches)
    if names != SECTION_NAMES:
        raise NotebookProbeError(
            "笔记本必须按固定顺序且各一次包含四个节标题"
        )
    if candidate[: matches[0].start()].strip():
        raise NotebookProbeError("四节之前出现了额外文本")
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(candidate)
        sections[match.group(1)] = candidate[match.end() : end].strip()
    normalized = "\n\n".join(
        f"【{name}】\n{sections[name] or '暂无'}" for name in SECTION_NAMES
    )
    return ParsedNotebook(normalized, sections)


def _normalized_items(section: str) -> tuple[str, ...]:
    items: list[str] = []
    for raw in section.splitlines():
        item = LIST_PREFIX_PATTERN.sub("", raw).strip()
        if not item or item in {"暂无", "无", "无。"}:
            continue
        items.append(item)
    return tuple(items)


def _change_items(section: str, heading: str) -> tuple[str, ...]:
    current: str | None = None
    found: list[str] = []
    for raw in section.splitlines():
        heading_match = CHANGE_HEADING_PATTERN.match(raw)
        if heading_match:
            current = heading_match.group(1)
            inline = heading_match.group(2).strip()
            if current == heading and inline not in {"", "暂无", "无", "无。"}:
                found.append(inline)
            continue
        if current == heading:
            item = LIST_PREFIX_PATTERN.sub("", raw).strip()
            if item and item not in {"暂无", "无", "无。"}:
                found.append(item)
    return tuple(found)


def _stable_counts(section: str) -> tuple[int, int]:
    lines = [line.strip() for line in section.splitlines() if line.strip()]
    items = [line for line in lines if STABLE_ITEM_PATTERN.match(line)]
    return len(items), sum("状态：已印证" in item for item in items)


def _deduplicate(items: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = "".join(item.split()).casefold().rstrip("。.;；")
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return tuple(result)


def _reported_cost(
    result: LlmResult,
    input_price: float,
    output_price: float,
) -> tuple[float | None, str]:
    if result.usage.cost_usd is not None:
        return result.usage.cost_usd, "upstream"
    if (
        result.usage.prompt_tokens is not None
        and result.usage.completion_tokens is not None
    ):
        return (
            (
                result.usage.prompt_tokens * input_price
                + result.usage.completion_tokens * output_price
            )
            / 1_000_000.0,
            "configured_prices",
        )
    return None, "unknown"


def _estimated_tokens(text: str) -> int:
    return max(1, math.ceil(len(text.encode("utf-8")) / 3))


def _estimated_call_cost(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    input_price: float,
    output_price: float,
) -> float:
    return (
        _estimated_tokens(system_prompt + user_prompt) * input_price
        + max_tokens * output_price
    ) / 1_000_000.0


def _failure_snapshot(kind: str, error: str, raw: str) -> str:
    return (
        f"# {kind}\n\n"
        f"错误：{error}\n\n"
        "## 模型原文\n\n"
        f"{raw if raw else '（无模型原文）'}\n"
    )


def run_combination(
    prepared: PreparedLog,
    profile_name: str,
    llm_configuration: LlmConfig,
    system_prompt: str,
    output_directory: Path,
    window_size: int,
    budget: CostBudget,
    *,
    client: LlmClientProtocol,
) -> CombinationSummary:
    effective = resolve_llm_profile(llm_configuration, profile_name)
    profile = llm_configuration.profiles.get(profile_name)
    if profile is None:
        raise NotebookProbeError(f"模型档位不存在：{profile_name}")
    if (
        profile.input_price_per_million_usd is None
        or profile.output_price_per_million_usd is None
    ):
        raise NotebookProbeError(f"模型档位 {profile_name} 未配置输入/输出单价")
    input_price = profile.input_price_per_million_usd
    output_price = profile.output_price_per_million_usd
    output_directory.mkdir(parents=True, exist_ok=False)
    current_notebook = parse_notebook(EMPTY_NOTEBOOK)
    gap_items: list[str] = []
    unresolved_items: list[str] = []
    stats: list[dict[str, object]] = []
    total_cost = 0.0
    total_latency_ms = 0.0
    rejected_count = 0
    parse_failures = 0
    api_failures = 0
    stopped_by_cost_guard = False

    for window_index, entries in enumerate(
        split_windows(prepared.entries, window_size), start=1
    ):
        user_prompt = build_user_message(
            prepared.game, current_notebook.text, entries
        )
        estimated_cost = _estimated_call_cost(
            system_prompt,
            user_prompt,
            effective.max_tokens,
            input_price,
            output_price,
        )
        snapshot_name = f"notebook-w{window_index:02d}.md"
        window_stat: dict[str, object] = {
            "window": window_index,
            "seq_start": entries[0].seq,
            "seq_end": entries[-1].seq,
            "entry_count": len(entries),
            "snapshot": snapshot_name,
            "estimated_call_cost_usd": estimated_cost,
        }
        if not budget.can_start(estimated_cost):
            stopped_by_cost_guard = True
            window_stat.update(
                {
                    "called": False,
                    "parse_success": False,
                    "error": "cost_cap",
                }
            )
            stats.append(window_stat)
            (output_directory / snapshot_name).write_text(
                _failure_snapshot(
                    "费用护栏停止",
                    f"下一窗估算后累计将超过 ${budget.cap_usd:.6f}",
                    "",
                ),
                encoding="utf-8",
            )
            break
        try:
            result = client.complete(
                model=effective.model,
                provider=effective.provider or None,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=effective.max_tokens,
                temperature=effective.temperature,
                reasoning_enabled=False,
            )
        except (LlmError, ValueError, OSError) as error:
            api_failures += 1
            latency_ms = (
                error.latency_seconds * 1000.0
                if isinstance(error, LlmError) and error.latency_seconds is not None
                else None
            )
            window_stat.update(
                {
                    "called": True,
                    "parse_success": False,
                    "input_tokens": None,
                    "output_tokens": None,
                    "reasoning_tokens": None,
                    "latency_ms": latency_ms,
                    "cost_usd": None,
                    "cost_source": "unknown",
                    "error": f"api:{error}",
                }
            )
            stats.append(window_stat)
            budget.charge(None, estimated_cost)
            (output_directory / snapshot_name).write_text(
                _failure_snapshot("调用失败", str(error), ""), encoding="utf-8"
            )
            continue

        latency_ms = result.latency_seconds * 1000.0
        total_latency_ms += latency_ms
        cost, cost_source = _reported_cost(result, input_price, output_price)
        budget.charge(cost, estimated_cost)
        if cost is not None:
            total_cost += cost
        window_stat.update(
            {
                "called": True,
                "input_tokens": result.usage.prompt_tokens,
                "output_tokens": result.usage.completion_tokens,
                "reasoning_tokens": result.usage.reasoning_tokens,
                "latency_ms": latency_ms,
                "cost_usd": cost,
                "cost_source": cost_source,
                "finish_reason": result.finish_reason,
                "actual_model": result.model,
                "actual_provider": result.provider,
            }
        )
        try:
            parsed = parse_notebook(result.text)
        except NotebookProbeError as error:
            parse_failures += 1
            window_stat.update({"parse_success": False, "error": f"parse:{error}"})
            (output_directory / snapshot_name).write_text(
                _failure_snapshot("解析失败", str(error), result.text),
                encoding="utf-8",
            )
        else:
            current_notebook = parsed
            rejected_count += len(_change_items(parsed.sections["本轮变动"], "驳回"))
            gap_items.extend(_normalized_items(parsed.sections["信息缺口"]))
            unresolved_items.extend(_normalized_items(parsed.sections["未决问题"]))
            window_stat.update({"parse_success": True, "error": None})
            (output_directory / snapshot_name).write_text(
                parsed.text + "\n", encoding="utf-8"
            )
        stats.append(window_stat)

    (output_directory / "notebook.md").write_text(
        current_notebook.text + "\n", encoding="utf-8"
    )
    deduplicated_gaps = _deduplicate(gap_items)
    deduplicated_unresolved = _deduplicate(unresolved_items)
    gaps_lines = ["# 窗口问题汇总", "", "【信息缺口】"]
    gaps_lines.extend(
        [f"- {item}" for item in deduplicated_gaps] or ["暂无"]
    )
    gaps_lines.extend(["", "【未决问题】"])
    gaps_lines.extend(
        [f"- {item}" for item in deduplicated_unresolved] or ["暂无"]
    )
    (output_directory / "gaps.md").write_text(
        "\n".join(gaps_lines) + "\n", encoding="utf-8"
    )
    final_count, confirmed_count = _stable_counts(
        current_notebook.sections["稳定认知"]
    )
    stats_payload = {
        "log": str(prepared.path),
        "log_sha256_before": prepared.sha256,
        "log_sha256_after": _file_sha256(prepared.path),
        "profile": profile_name,
        "configured_model": effective.model,
        "window_size": window_size,
        "window_count": len(split_windows(prepared.entries, window_size)),
        "windows": stats,
        "totals": {
            "input_tokens": sum(
                int(item["input_tokens"])
                for item in stats
                if isinstance(item.get("input_tokens"), int)
            ),
            "output_tokens": sum(
                int(item["output_tokens"])
                for item in stats
                if isinstance(item.get("output_tokens"), int)
            ),
            "latency_ms": total_latency_ms,
            "cost_usd": total_cost,
            "parse_failures": parse_failures,
            "api_failures": api_failures,
            "stopped_by_cost_guard": stopped_by_cost_guard,
        },
    }
    (output_directory / "stats.json").write_text(
        json.dumps(stats_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return CombinationSummary(
        log_name=prepared.name,
        profile=profile_name,
        output_directory=output_directory,
        total_cost_usd=total_cost,
        total_latency_ms=total_latency_ms,
        final_cognition_count=final_count,
        confirmed_count=confirmed_count,
        rejected_count=rejected_count,
        information_gaps=deduplicated_gaps,
        parse_failure_count=parse_failures,
        api_failure_count=api_failures,
        stopped_by_cost_guard=stopped_by_cost_guard,
    )


def _preflight_estimate(
    prepared_logs: Sequence[PreparedLog],
    profiles: Sequence[str],
    configuration: LlmConfig,
    system_prompt: str,
    window_size: int,
) -> tuple[float, dict[tuple[str, str], float]]:
    estimates: dict[tuple[str, str], float] = {}
    for prepared in prepared_logs:
        for profile_name in profiles:
            effective = resolve_llm_profile(configuration, profile_name)
            profile = configuration.profiles.get(profile_name)
            if profile is None:
                raise NotebookProbeError(f"模型档位不存在：{profile_name}")
            if (
                profile.input_price_per_million_usd is None
                or profile.output_price_per_million_usd is None
            ):
                raise NotebookProbeError(
                    f"模型档位 {profile_name} 未配置输入/输出单价"
                )
            notebook_placeholder = EMPTY_NOTEBOOK
            total = 0.0
            for entries in split_windows(prepared.entries, window_size):
                message = build_user_message(prepared.game, notebook_placeholder, entries)
                total += _estimated_call_cost(
                    system_prompt,
                    message,
                    effective.max_tokens,
                    profile.input_price_per_million_usd,
                    profile.output_price_per_million_usd,
                )
                notebook_placeholder = "占" * (effective.max_tokens * 2)
            estimates[(prepared.name, profile_name)] = total
    return sum(estimates.values()), estimates


def _write_review(
    output_root: Path,
    summaries: Sequence[CombinationSummary],
    prompt_text: str,
    prepared_logs: Sequence[PreparedLog],
    estimated_cost: float,
    budget: CostBudget,
) -> None:
    lines = [
        "# M5-B-T1 笔记本探针汇总",
        "",
        "本报告只汇总机械结果和模型原文，不评价模型优劣。",
        "",
        "## 档位 × 日志矩阵",
        "",
        "| 档位 | 日志 | 总花费 | 总时延 ms | 最终认知 | 已印证 | 被驳回 | 解析失败 | API 失败 | 费用护栏 | 信息缺口条目 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for summary in summaries:
        gaps = "<br>".join(item.replace("|", "\\|") for item in summary.information_gaps)
        lines.append(
            f"| {summary.profile} | {summary.log_name} | "
            f"${summary.total_cost_usd:.9f} | {summary.total_latency_ms:.3f} | "
            f"{summary.final_cognition_count} | {summary.confirmed_count} | "
            f"{summary.rejected_count} | {summary.parse_failure_count} | "
            f"{summary.api_failure_count} | "
            f"{'停止' if summary.stopped_by_cost_guard else '未触发'} | {gaps or '暂无'} |"
        )
    lines.extend(
        [
            "",
            "## 费用与只读校验",
            "",
            f"- 运行前预计总花费：${estimated_cost:.9f}",
            f"- 已知实际总花费：${budget.known_actual_usd:.9f}",
            f"- 费用未知窗口数：{budget.unknown_cost_windows}",
            f"- 费用上限：${budget.cap_usd:.9f}",
        ]
    )
    for prepared in prepared_logs:
        after = _file_sha256(prepared.path)
        lines.append(
            f"- `{prepared.path}`：前 `{prepared.sha256}`；后 `{after}`；"
            f"一致：{prepared.sha256 == after}"
        )
    lines.extend(["", "## 四组产物", ""])
    for summary in summaries:
        relative = summary.output_directory.relative_to(output_root).as_posix()
        lines.append(
            f"- `{relative}`：`notebook.md`、`gaps.md`、逐窗 `notebook-wNN.md`、`stats.json`"
        )
    lines.extend(
        [
            "",
            "## notebook-probe.md 全文",
            "",
            "```text",
            prompt_text.rstrip(),
            "```",
            "",
        ]
    )
    (output_root / "review.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[str, LlmConfig], LlmClientProtocol] | None = None,
) -> int:
    _configure_console()
    arguments = build_parser().parse_args(argv)
    if arguments.window < 1 or not math.isfinite(arguments.cost_cap) or arguments.cost_cap <= 0:
        print("--window 与 --cost-cap 必须为正数", file=sys.stderr)
        return 2
    try:
        configuration = load_config()
        prompt_text = arguments.prompt.resolve().read_text(encoding="utf-8")
        prepared_logs = tuple(load_observation_log(path) for path in arguments.log)
        profiles = tuple(dict.fromkeys(arguments.profile))
        if len(profiles) != len(arguments.profile):
            raise NotebookProbeError("--profile 不得重复")
        for profile_name in profiles:
            effective = resolve_llm_profile(configuration.llm, profile_name)
            if not effective.model.strip():
                raise NotebookProbeError(f"模型档位 {profile_name} 未配置型号")
            if not os.environ.get(effective.api_key_env, "").strip() and client_factory is None:
                raise NotebookProbeError(
                    f"模型档位 {profile_name} 缺少环境变量 {effective.api_key_env}；未回退"
                )
        estimated_cost, estimates = _preflight_estimate(
            prepared_logs,
            profiles,
            configuration.llm,
            prompt_text,
            arguments.window,
        )
        print("即将把以下观察日志原文发送给配置中的文字档位：")
        for prepared in prepared_logs:
            print(f"  - {prepared.path}（{len(prepared.entries)} 条，SHA-256 {prepared.sha256}）")
        for profile_name in profiles:
            effective = resolve_llm_profile(configuration.llm, profile_name)
            print(f"档位：{profile_name}（型号来自配置：{effective.model}）")
        for (log_name, profile_name), value in estimates.items():
            print(f"预计 {log_name} × {profile_name}：${value:.6f}")
        print(f"预计总花费：${estimated_cost:.6f}；花费上限：${arguments.cost_cap:.6f}")
        if estimated_cost > arguments.cost_cap:
            raise NotebookProbeError(
                f"预计总花费 ${estimated_cost:.6f} 超过 --cost-cap ${arguments.cost_cap:.6f}"
            )
        if not arguments.yes and input("确认发送？请输入 YES 继续：").strip() != "YES":
            print("未确认，未发送任何日志。")
            return 0
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        output_root = arguments.output_root / f"notebook-probe-{stamp}"
        output_root.mkdir(parents=True, exist_ok=False)
        budget = CostBudget(arguments.cost_cap)
        summaries: list[CombinationSummary] = []
        for prepared in prepared_logs:
            for profile_name in profiles:
                effective = resolve_llm_profile(configuration.llm, profile_name)
                owns_client = client_factory is None
                client = (
                    OpenRouterClient.from_profile(
                        profile_name=profile_name,
                        base_url=effective.base_url,
                        api_key_env=effective.api_key_env,
                        timeout_seconds=effective.timeout_seconds,
                    )
                    if client_factory is None
                    else client_factory(profile_name, effective)
                )
                try:
                    summaries.append(
                        run_combination(
                            prepared,
                            profile_name,
                            configuration.llm,
                            prompt_text,
                            output_root / f"{prepared.name}-{_slug(profile_name)}",
                            arguments.window,
                            budget,
                            client=client,
                        )
                    )
                finally:
                    if owns_client and isinstance(client, OpenRouterClient):
                        client.close()
        _write_review(
            output_root,
            summaries,
            prompt_text,
            prepared_logs,
            estimated_cost,
            budget,
        )
        print(f"输出：{output_root}")
        print(f"已知实际花费：${budget.known_actual_usd:.9f}")
        return 0
    except (
        NotebookProbeError,
        OSError,
        ValueError,
        tomllib.TOMLDecodeError,
    ) as error:
        print(f"笔记本探针未执行：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
