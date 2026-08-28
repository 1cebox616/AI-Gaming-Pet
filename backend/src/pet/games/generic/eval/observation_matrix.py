"""Run the standing multi-profile generic-vision replay matrix."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import statistics
import sys
import tomllib

from pet.core.config import AdapterConfig, LlmConfig, load_config, resolve_llm_profile
from pet.core.llm import LlmError, LlmModelEndpoint, fetch_model_endpoints
from pet.games.generic.adapter import WindowTitleMap
from pet.games.generic.eval.observation_replay import (
    BACKEND_DIRECTORY,
    DEFAULT_COST_CAP_USD,
    DEFAULT_OUTPUT_ROOT,
    ESTIMATED_INPUT_TOKENS_PER_CALL,
    PRODUCTION_SEND_WIDTH,
    ObservationReplayError,
    PreparedReplay,
    SegmentRange,
    _echoed_metric_values,
    _extract_local,
    _extract_speculation,
    _input_attribution_violation,
    _local_statistics,
    _percentile,
    _prepare_replay,
    _read_rows,
    _recording_hash,
    _retrospective_violation,
    _run_prepared,
    _write_review,
)

DEFAULT_MANIFEST = BACKEND_DIRECTORY / "data" / "generic" / "replay-truth" / "m5-t8-segments.toml"


@dataclass(frozen=True, slots=True)
class MatrixRole:
    name: str
    session: Path
    segment: SegmentRange | None
    category: str


@dataclass(frozen=True, slots=True)
class LiveProfile:
    name: str
    model: str
    provider: str | None
    fetched_at: str
    endpoints: tuple[LlmModelEndpoint, ...]
    input_price_per_million_usd: float
    output_price_per_million_usd: float


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通用视觉双模型常设重放矩阵")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--profile", action="append", required=True)
    parser.add_argument("--role", action="append")
    parser.add_argument("--send-width", action="append", type=int)
    parser.add_argument("--max-inflight", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--dispatch-interval", type=float, default=1.0)
    parser.add_argument("--cost-cap", type=float, default=DEFAULT_COST_CAP_USD)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser


def _load_roles(path: Path, selected: Sequence[str] | None) -> tuple[MatrixRole, ...]:
    with path.open("rb") as stream:
        payload = tomllib.load(stream)
    wanted = set(selected or ())
    roles: list[MatrixRole] = []
    for item in payload.get("segments", []):
        name = str(item["role"])
        if wanted and name not in wanted:
            continue
        session = (path.parent / str(item["session"])).resolve()
        segment = None
        if "start" in item or "end" in item:
            segment = SegmentRange(float(item.get("start", 0.0)), float(item["end"]))
        roles.append(MatrixRole(name, session, segment, str(item.get("category", ""))))
    missing = wanted - {item.name for item in roles}
    if missing:
        raise ObservationReplayError(f"片段清单缺少角色：{sorted(missing)}")
    if not roles:
        raise ObservationReplayError("片段清单没有可运行角色")
    return tuple(roles)


def _live_profile(configuration: LlmConfig, name: str) -> LiveProfile:
    profile = configuration.profiles.get(name)
    if profile is None:
        raise ObservationReplayError(f"模型档位不存在：{name}")
    effective = resolve_llm_profile(configuration, name)
    if not effective.model.strip():
        raise ObservationReplayError(f"模型档位 {name} 未配置型号")
    if effective.base_url is None and not (profile.provider or "").strip():
        raise ObservationReplayError(f"模型档位 {name} 未锁定单一上游，拒绝进入矩阵")
    if not os.environ.get(effective.api_key_env, "").strip():
        raise ObservationReplayError(f"模型档位 {name} 缺少环境变量 {effective.api_key_env}")
    advertised = fetch_model_endpoints(
        profile_name=name,
        base_url=effective.base_url,
        api_key_env=effective.api_key_env,
        timeout_seconds=effective.timeout_seconds,
        model=effective.model,
    )
    if advertised is None:
        raise ObservationReplayError(f"模型档位 {name} 的端点未提供可解析的实时单价目录")
    provider = (profile.provider or "").strip() or None
    matched = tuple(
        item
        for item in advertised
        if provider is None or item.provider.casefold() == provider.casefold()
    )
    if not matched:
        raise ObservationReplayError(f"模型档位 {name} 的锁定上游不在实时目录中")
    input_price = min(item.prompt_price_per_token for item in matched) * 1_000_000
    output_price = min(item.completion_price_per_token for item in matched) * 1_000_000
    return LiveProfile(
        name,
        effective.model,
        provider,
        datetime.now(timezone.utc).isoformat(),
        matched,
        input_price,
        output_price,
    )


def _configuration_with_live_prices(
    configuration: LlmConfig,
    live: LiveProfile,
) -> LlmConfig:
    selected = configuration.profiles[live.name].model_copy(
        update={
            "input_price_per_million_usd": live.input_price_per_million_usd,
            "output_price_per_million_usd": live.output_price_per_million_usd,
        }
    )
    return configuration.model_copy(
        update={"profiles": {**configuration.profiles, live.name: selected}}
    )


def _summary(directory: Path) -> dict[str, object]:
    rows = _read_rows(directory)
    successful = [row for row in rows if row.get("dropped") is None]
    local = _local_statistics(rows)
    ttft = [float(row["ttft_ms"]) for row in successful if row.get("ttft_ms") is not None]
    latency = [float(row["latency_ms"]) for row in successful]
    input_tokens = [int(row["input_tokens"]) for row in successful if row.get("input_tokens") is not None]
    focused = [row for row in successful if row.get("region_area_ratio") is not None]
    paired = sum(_extract_local(str(row.get("text", ""))) is not None for row in focused)
    session = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    dropped = Counter(str(row["dropped"]) for row in rows if row.get("dropped") is not None)
    return {
        "attempts": len(rows),
        "success": len(successful),
        "cost_usd": float(session["total_cost_usd"]),
        "input_tokens_mean": statistics.mean(input_tokens) if input_tokens else None,
        "ttft_median_ms": statistics.median(ttft) if ttft else None,
        "ttft_p90_ms": _percentile(ttft, 0.90),
        "latency_median_ms": statistics.median(latency) if latency else None,
        "latency_p90_ms": _percentile(latency, 0.90),
        "only_prefix_old": local.only_prefix_old,
        "only_compliant": local.only_compliant,
        "only_expanded": local.only_expanded,
        "only_compliance_rate": (
            local.only_compliant / local.only_prefix_old if local.only_prefix_old else None
        ),
        "numeric_local": local.numeric,
        "numeric_local_rate": local.numeric / local.total if local.total else None,
        "grid_leaks": sum(
            1
            for row in successful
            if re.search(r"(?i)r\d+c\d+", str(row.get("text", "")))
        ),
        "focused_pair_rate": paired / len(focused) if focused else None,
        "input_attribution_violations": sum(_input_attribution_violation(row) for row in successful),
        "retrospective_violations": sum(_retrospective_violation(row) for row in successful),
        "metric_echoes": sum(bool(_echoed_metric_values(row)) for row in successful),
        "speculation_count": sum(
            _extract_speculation(str(row.get("text", ""))) is not None for row in successful
        ),
        "rate_limited": sum(count for reason, count in dropped.items() if "429" in reason),
        "timeouts": dropped.get("timeout", 0),
        "truncated": int(session.get("truncated_count", 0)),
    }


def _format_optional(value: object, *, percentage: bool = False) -> str:
    if value is None:
        return "—"
    number = float(value)
    return f"{number:.2%}" if percentage else f"{number:.1f}"


def _write_matrix_review(root: Path, groups: Sequence[dict[str, object]]) -> None:
    lines = [
        "# 通用视觉常设矩阵",
        "",
        "所有列使用同一提示词、检测器与并发参数；档位均锁定单一上游。表格只给数据，不评价模型优劣。",
        "旧‘仅’列只判前缀；严格合规要求完整【局部】正文为 4–6 个汉字且以‘仅’开头。",
        "",
        "| 档位 | 角色 | 宽度 | 成功/尝试 | 花费 | 输入token均值 | TTFT中位/P90 ms | 总时延中位/P90 ms | 数字局部 | 严格仅/展开 | 推测 | 归因/回溯/指标复述 | 429/超时 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for group in groups:
        stats = group["stats"]
        assert isinstance(stats, dict)
        lines.append(
            f"| {group['profile']} | {group['role']} | {group['width']} | "
            f"{stats['success']}/{stats['attempts']} | ${float(stats['cost_usd']):.6f} | "
            f"{_format_optional(stats['input_tokens_mean'])} | "
            f"{_format_optional(stats['ttft_median_ms'])}/{_format_optional(stats['ttft_p90_ms'])} | "
            f"{_format_optional(stats['latency_median_ms'])}/{_format_optional(stats['latency_p90_ms'])} | "
            f"{stats['numeric_local']} ({_format_optional(stats['numeric_local_rate'], percentage=True)}) | "
            f"{stats['only_compliant']}/{stats['only_expanded']} | {stats['speculation_count']} | "
            f"{stats['input_attribution_violations']}/{stats['retrospective_violations']}/{stats['metric_echoes']} | "
            f"{stats['rate_limited']}/{stats['timeouts']} |"
        )
    lines.extend(["", "## 实时目录与锁定证明", ""])
    run = json.loads((root / "run.json").read_text(encoding="utf-8"))
    for profile in run["profiles"]:
        lines.append(
            f"- {profile['profile']}：型号 `{profile['model']}`；锁定上游 "
            f"`{profile['provider']}`；获取时间 {profile['fetched_at']}；"
            f"输入/输出单价 ${profile['input_price_per_million_usd']}/"
            f"${profile['output_price_per_million_usd']} 每百万 token。"
        )
    (root / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _endpoint_json(item: LlmModelEndpoint) -> dict[str, object]:
    return {
        "name": item.name,
        "provider": item.provider,
        "prompt_price_per_token": item.prompt_price_per_token,
        "completion_price_per_token": item.completion_price_per_token,
        "context_length": item.context_length,
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    widths = tuple(arguments.send_width or (PRODUCTION_SEND_WIDTH,))
    if (
        any(width <= 0 for width in widths)
        or arguments.max_inflight < 1
        or arguments.timeout <= 0
        or arguments.dispatch_interval < 0
        or arguments.cost_cap <= 0
    ):
        print("宽度、并发、超时与花费参数无效", file=sys.stderr)
        return 2
    try:
        configuration = load_config()
        generic = configuration.games.get("generic", AdapterConfig()).generic
        roles = _load_roles(arguments.manifest, arguments.role)
        lives = tuple(_live_profile(configuration.llm, name) for name in arguments.profile)
        title_map = WindowTitleMap.load()
        prepared: dict[tuple[str, int], PreparedReplay] = {}
        for role in roles:
            for width in widths:
                prepared[(role.name, width)] = replace(
                    _prepare_replay(
                        role.session,
                        width,
                        role.segment,
                        title_map,
                        generic.region_focus_max,
                    ),
                    name=role.name,
                )
        estimated_cost = 0.0
        for live in lives:
            for item in prepared.values():
                effective = resolve_llm_profile(configuration.llm, live.name)
                estimated_cost += len(item.selected) * (
                    ESTIMATED_INPUT_TOKENS_PER_CALL * live.input_price_per_million_usd
                    + effective.max_tokens * live.output_price_per_million_usd
                ) / 1_000_000
        print(f"矩阵组合：{len(lives)} 档位 × {len(roles)} 角色 × {len(widths)} 宽度")
        for live in lives:
            print(
                f"{live.name}: {live.model}；锁定 {live.provider or '自定义单端点'}；"
                f"实时输入/输出单价 ${live.input_price_per_million_usd:g}/"
                f"${live.output_price_per_million_usd:g} 每百万 token"
            )
        print(f"预计花费：${estimated_cost:.6f}；花费上限：${arguments.cost_cap:.6f}")
        if estimated_cost > arguments.cost_cap:
            raise ObservationReplayError("预计花费超过 --cost-cap")
        if not arguments.yes and input("确认执行？请输入 YES 继续：").strip() != "YES":
            print("未确认，未调用 API。")
            return 0
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        root = arguments.output_root / f"observation-matrix-{stamp}"
        root.mkdir(parents=True, exist_ok=False)
        groups: list[dict[str, object]] = []
        running_cost = 0.0
        hashes_before = {str(role.session): _recording_hash(role.session) for role in roles}
        for live in lives:
            priced = _configuration_with_live_prices(configuration.llm, live)
            for width in widths:
                review_items: list[PreparedReplay] = []
                group_root = root / live.name / f"w{width}"
                for role in roles:
                    item = prepared[(role.name, width)]
                    review_items.append(item)
                    stopped = asyncio.run(
                        _run_prepared(
                            item,
                            group_root / role.name,
                            llm_configuration=priced,
                            profile=live.name,
                            max_inflight=arguments.max_inflight,
                            timeout=arguments.timeout,
                            region_focus_max=generic.region_focus_max,
                            cost_cap=arguments.cost_cap,
                            prior_cost=running_cost,
                            dispatch_interval=arguments.dispatch_interval,
                        )
                    )
                    stats = _summary(group_root / role.name)
                    running_cost += float(stats["cost_usd"])
                    groups.append(
                        {
                            "profile": live.name,
                            "role": role.name,
                            "category": role.category,
                            "width": width,
                            "directory": str((group_root / role.name).relative_to(root)),
                            "stats": stats,
                        }
                    )
                    if stopped:
                        raise ObservationReplayError("运行中花费护栏触发")
                _write_review(group_root, review_items, arguments.manifest)
        hashes_after = {str(role.session): _recording_hash(role.session) for role in roles}
        run = {
            "started_output_timestamp": stamp,
            "parameters": {
                "max_inflight": arguments.max_inflight,
                "timeout_seconds": arguments.timeout,
                "dispatch_interval_seconds": arguments.dispatch_interval,
                "cost_cap_usd": arguments.cost_cap,
                "widths": widths,
            },
            "profiles": [
                {
                    "profile": live.name,
                    "model": live.model,
                    "provider": live.provider,
                    "fetched_at": live.fetched_at,
                    "input_price_per_million_usd": live.input_price_per_million_usd,
                    "output_price_per_million_usd": live.output_price_per_million_usd,
                    "catalog_entries": [_endpoint_json(item) for item in live.endpoints],
                }
                for live in lives
            ],
            "estimated_cost_usd": estimated_cost,
            "actual_cost_usd": running_cost,
            "groups": groups,
            "recording_hashes": {
                path: {
                    "before": value,
                    "after": hashes_after[path],
                    "matches": value == hashes_after[path],
                }
                for path, value in hashes_before.items()
            },
        }
        (root / "run.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        _write_matrix_review(root, groups)
        print(f"输出：{root}")
        print(f"实际花费：${running_cost:.9f}")
        return 0
    except (LlmError, ObservationReplayError, OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"观察矩阵未执行：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
