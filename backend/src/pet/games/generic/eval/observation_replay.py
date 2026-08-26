"""Replay retained raw frames through the production generic visual observer."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tomllib

from PIL import Image

from pet.core.capture import (
    AdaptiveFrameSelector,
    CapturedFrame,
    FrameMetadata,
    ReplayFrameTime,
    _load_replay_frame_times,
    _replay_session_monotonic_origin,
)
from pet.core.config import (
    AdapterConfig,
    GenericVisionConfig,
    LlmConfig,
    load_config,
    resolve_llm_profile,
)
from pet.games.generic.adapter import GenericVisionAdapter, WindowTitleMap

BACKEND_DIRECTORY = Path(__file__).resolve().parents[5]
DEFAULT_OUTPUT_ROOT = BACKEND_DIRECTORY / "eval-reports"
DEFAULT_SEGMENTS_PATH = BACKEND_DIRECTORY / "audit" / "m5-t6-segments.toml"
PRODUCTION_SEND_WIDTH = 896
DEFAULT_COST_CAP_USD = 5.0
ESTIMATED_INPUT_TOKENS_PER_CALL = 4_000


class ObservationReplayError(Exception):
    """A replay input or configuration error safe to show to the operator."""


@dataclass(frozen=True, slots=True)
class SegmentRange:
    start: float
    end: float | None

    def contains(self, value: float) -> bool:
        return value >= self.start and (self.end is None or value < self.end)


@dataclass(frozen=True, slots=True)
class ReplayFrameSpec:
    path: Path
    timing: ReplayFrameTime
    relative_seconds: float
    region: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedReplay:
    session: Path
    name: str
    game: str
    title: str
    process_name: str
    frame_count: int
    selected: tuple[ReplayFrameSpec, ...]
    source_width: int
    source_height: int
    requested_send_width: int
    actual_send_width: int
    recording_hash: str


def _parse_segment(value: str) -> SegmentRange:
    parts = tuple(part.strip() for part in value.split(","))
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--segment 格式必须为 起,止")
    try:
        start = float(parts[0])
        end = float(parts[1])
    except ValueError as error:
        raise argparse.ArgumentTypeError("--segment 起止必须是秒数") from error
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise argparse.ArgumentTypeError("--segment 必须满足 0 <= 起 < 止")
    return SegmentRange(start, end)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M5-T7.2 离线快线观察日志重放")
    parser.add_argument("--session", action="append", required=True, type=Path)
    parser.add_argument("--segment", type=_parse_segment)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--context-lines", type=int, default=5)
    parser.add_argument("--max-inflight", type=int, default=4)
    parser.add_argument("--send-width", type=int, default=PRODUCTION_SEND_WIDTH)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--dispatch-interval", type=float, default=0.0)
    parser.add_argument("--cost-cap", type=float, default=DEFAULT_COST_CAP_USD)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser


def _session_title(payload: dict[str, object]) -> str:
    startup = payload.get("启动参数")
    if isinstance(startup, dict):
        title = startup.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    raise ObservationReplayError("session.json 缺少启动参数.title，无法确定游戏上下文")


def _session_label(payload: dict[str, object], session: Path) -> str:
    label = payload.get("标签")
    if isinstance(label, str) and label.strip():
        cleaned = "-".join(label.strip().replace("_", "-").split())
        return f"{cleaned}-{session.name}"
    return session.name


def _recording_hash(session: Path) -> str:
    """Hash only the read-only recording inputs used or guarded by this task."""
    digest = hashlib.sha256()
    paths = sorted((session / "raw").glob("raw-*.jpg"))
    input_path = session / "input.csv"
    if input_path.is_file():
        paths.append(input_path)
    for path in paths:
        digest.update(path.relative_to(session).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _prepare_replay(
    session: Path,
    requested_width: int,
    segment: SegmentRange | None,
    title_map: WindowTitleMap,
    region_sparsity_max: float,
) -> PreparedReplay:
    session = session.resolve()
    raw_dir = session / "raw"
    paths = sorted(raw_dir.glob("raw-*.jpg"))
    if not paths:
        raise ObservationReplayError(f"会话没有 raw JPEG：{raw_dir}")
    try:
        payload = json.loads((session / "session.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ObservationReplayError(f"无法读取 {session / 'session.json'}：{error}") from error
    if not isinstance(payload, dict):
        raise ObservationReplayError(f"session.json 顶层不是对象：{session}")
    timings = _load_replay_frame_times(raw_dir, paths)
    origin = _replay_session_monotonic_origin(raw_dir, timings)
    title = _session_title(payload)
    process_name = ""
    game = title_map.identify(title, process_name)
    selector = AdaptiveFrameSelector(region_sparsity_max=region_sparsity_max)
    selected: list[ReplayFrameSpec] = []
    source_size: tuple[int, int] | None = None
    included_count = 0
    for path, timing in zip(paths, timings, strict=True):
        relative = timing.monotonic_seconds - origin
        if segment is not None and not segment.contains(relative):
            continue
        included_count += 1
        with Image.open(path) as source:
            bitmap = source.convert("RGB")
        if source_size is None:
            source_size = bitmap.size
        elif bitmap.size != source_size:
            bitmap.close()
            raise ObservationReplayError(f"同一 raw 流出现不同尺寸：{path}")
        observation = selector.observe(bitmap, timing.monotonic_seconds)
        bitmap.close()
        if observation.decision.should_save:
            selected.append(
                ReplayFrameSpec(path, timing, relative, observation.decision.region_grid)
            )
    if not included_count or source_size is None:
        raise ObservationReplayError(f"指定区间没有帧：{session}")
    actual_width = min(requested_width, source_size[0])
    return PreparedReplay(
        session=session,
        name=_session_label(payload, session),
        game=game,
        title=title,
        process_name=process_name,
        frame_count=included_count,
        selected=tuple(selected),
        source_width=source_size[0],
        source_height=source_size[1],
        requested_send_width=requested_width,
        actual_send_width=actual_width,
        recording_hash=_recording_hash(session),
    )


def _adapter_configuration(
    *,
    profile: str,
    send_width: int,
    timeout: float,
    max_inflight: int,
    region_sparsity_max: float,
) -> AdapterConfig:
    return AdapterConfig(
        generic=GenericVisionConfig(
            enabled=True,
            send_width=send_width,
            fast_timeout_seconds=timeout,
            max_inflight=max_inflight,
            region_sparsity_max=region_sparsity_max,
            llm_profile=profile,
        )
    )


async def _run_prepared(
    prepared: PreparedReplay,
    output_directory: Path,
    *,
    llm_configuration: LlmConfig,
    profile: str,
    context_lines: int,
    max_inflight: int,
    timeout: float,
    region_sparsity_max: float,
    cost_cap: float,
    prior_cost: float,
    dispatch_interval: float,
) -> bool:
    adapter = GenericVisionAdapter(
        _adapter_configuration(
            profile=profile,
            send_width=prepared.actual_send_width,
            timeout=timeout,
            max_inflight=max_inflight,
            region_sparsity_max=region_sparsity_max,
        ),
        llm_configuration,
        capture_backend_factory=lambda: (_ for _ in ()).throw(
            AssertionError("离线重放不得初始化实时截屏")
        ),
        selector_factory=lambda _sparsity: (_ for _ in ()).throw(
            AssertionError("离线重放的帧选择在只读 raw 流上完成")
        ),
    )
    adapter.start_replay(
        output_directory,
        context_lines=context_lines,
        extra_parameters={
            "mode": "offline_replay",
            "source_session": str(prepared.session),
            "source_dimensions": [prepared.source_width, prepared.source_height],
            "requested_send_width": prepared.requested_send_width,
            "actual_send_width": prepared.actual_send_width,
            "production_default_send_width": PRODUCTION_SEND_WIDTH,
            "width_difference_note": (
                f"raw 原图宽 {prepared.source_width}，实际上传 {prepared.actual_send_width}；"
                f"生产默认 {PRODUCTION_SEND_WIDTH}，本次未放大。"
            ),
        },
    )
    stopped_by_cost = False
    last_dispatch: float | None = None
    try:
        for item in prepared.selected:
            now = asyncio.get_running_loop().time()
            if last_dispatch is not None:
                remaining = dispatch_interval - (now - last_dispatch)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            with Image.open(item.path) as source:
                bitmap = source.convert("RGB")
            frame = CapturedFrame(
                bitmap,
                FrameMetadata(
                    prepared.title,
                    prepared.process_name,
                    item.timing.wall_time,
                    bitmap.width,
                    bitmap.height,
                    item.timing.monotonic_seconds,
                    item.timing.source,
                ),
            )
            await adapter.submit_replay_frame(frame, prepared.game, item.region)
            last_dispatch = asyncio.get_running_loop().time()
            if prior_cost + adapter.total_cost_usd > cost_cap * 1.5:
                stopped_by_cost = True
                print(
                    f"运行中累计花费 ${prior_cost + adapter.total_cost_usd:.6f} 超过中止线 "
                    f"${cost_cap * 1.5:.6f}；停止提交新帧。"
                )
                break
    finally:
        await adapter.finish_replay()
    return stopped_by_cost


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _read_rows(directory: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (directory / "observations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def character_similarity(left: str, right: str) -> float:
    """Return a deterministic character-level similarity ratio in 0..1."""
    return difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()


def _segments_for_session(path: Path, session: Path) -> tuple[tuple[str, SegmentRange], ...]:
    if not path.is_file():
        return ()
    with path.open("rb") as stream:
        payload = tomllib.load(stream)
    found: list[tuple[str, SegmentRange]] = []
    for item in payload.get("segments", []):
        source = (path.parent / str(item["session"])).resolve()
        if source != session.resolve():
            continue
        found.append(
            (
                str(item["role"]),
                SegmentRange(
                    float(item.get("start", 0.0)),
                    float(item["end"]) if "end" in item else None,
                ),
            )
        )
    return tuple(found)


def _write_review(
    output_root: Path,
    prepared_items: Sequence[PreparedReplay],
    segment_manifest: Path = DEFAULT_SEGMENTS_PATH,
) -> None:
    lines = [
        "# M5-T7.2 离线观察日志复核表",
        "",
        "本文件只列机器输出与机械统计，不评价模型能力。相似度为相邻文本的字符级 "
        "SequenceMatcher 重合率；超过 0.6 的相邻对列为复读候选，需产品负责人判读。",
        "",
    ]
    for prepared in prepared_items:
        directory = output_root / prepared.name
        rows = _read_rows(directory)
        session = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        latencies = [float(row["latency_ms"]) for row in rows if row.get("dropped") is None]
        dropped = Counter(str(row["dropped"]) for row in rows if row.get("dropped") is not None)
        injected = sum(1 for row in rows if row.get("region"))
        truncated = int(session.get("truncated_count", 0))
        unchanged = int(session.get("unchanged_count", 0))
        average_visible_tokens = session.get("average_visible_output_tokens")
        lines.extend(
            [
                f"## {prepared.name}",
                "",
                f"- 游戏上下文：{prepared.game}",
                f"- 轮询数：{prepared.frame_count}",
                f"- 上传数：{len(prepared.selected)}",
                f"- 上传率：{len(prepared.selected) / prepared.frame_count:.2%}",
                f"- 丢弃数：{sum(dropped.values())}；原因：{dict(dropped)}",
                (
                    f"- 往返中位 / P90：{statistics.median(latencies):.3f} / "
                    f"{(_percentile(latencies, 0.90) or 0.0):.3f} ms"
                    if latencies
                    else "- 往返中位 / P90：无成功调用"
                ),
                f"- 实际花费：${float(session['total_cost_usd']):.9f}",
                f"- 区域提示注入次数：{injected}",
                f"- 截断条数：{truncated}",
                f"- 「无明显变化」条数：{unchanged}",
                f"- 平均可见输出 token：{average_visible_tokens}",
                f"- 上传宽度：请求 {prepared.requested_send_width}，raw {prepared.source_width}，"
                f"实际 {prepared.actual_send_width}；未放大，低于生产默认。",
                "",
                "### 全部观察",
                "",
            ]
        )
        for row in rows:
            if row.get("dropped") is None:
                lines.append(f"- {row['wall']} · {row['game']}：{row['text']}")
            else:
                lines.append(f"- {row['wall']} · {row['game']}：[丢弃：{row['dropped']}]")
        lines.extend(["", "### 复读候选（相邻相似度 > 0.6）", ""])
        repeats = []
        successful = [row for row in rows if row.get("dropped") is None]
        for previous, current in zip(successful, successful[1:]):
            similarity = character_similarity(str(previous["text"]), str(current["text"]))
            if similarity > 0.6:
                repeats.append((previous, current, similarity))
        if repeats:
            lines.extend(
                [
                    "| 前一帧时间 | 当前帧时间 | 相似度 | 前文 | 后文 |",
                    "|---|---|---:|---|---|",
                ]
            )
            for previous, current, similarity in repeats:
                left = str(previous["text"]).replace("|", "\\|")
                right = str(current["text"]).replace("|", "\\|")
                lines.append(
                    f"| {previous['frame_ts']} | {current['frame_ts']} | "
                    f"{similarity:.3f} | {left} | {right} |"
                )
        else:
            lines.append("无。")
        segments = _segments_for_session(segment_manifest, prepared.session)
        if segments:
            first_timing = min(
                item.timing.monotonic_seconds - item.relative_seconds
                for item in prepared.selected
            )
            lines.extend(["", "### 机械区间分组小结", ""])
            for role, segment in segments:
                matching = [
                    row
                    for row in rows
                    if segment.contains(float(row["frame_ts"]) - first_timing)
                ]
                lines.append(
                    f"#### {role} [{segment.start:.1f}, "
                    f"{'全段' if segment.end is None else f'{segment.end:.1f}'})"
                )
                lines.append("")
                if matching:
                    for row in matching:
                        text = (
                            row["text"]
                            if row.get("dropped") is None
                            else f"[丢弃：{row['dropped']}]"
                        )
                        lines.append(f"- {float(row['frame_ts']) - first_timing:.3f}s：{text}")
                else:
                    lines.append("- 该机械区间没有观察条目。")
                lines.append("")
        lines.append("")
    (output_root / "review.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console()
    arguments = build_parser().parse_args(argv)
    if arguments.context_lines < 0 or arguments.max_inflight < 1:
        print("--context-lines 不得为负，--max-inflight 必须至少为 1", file=sys.stderr)
        return 2
    if (
        arguments.send_width < 1
        or arguments.timeout <= 0
        or arguments.cost_cap <= 0
        or arguments.dispatch_interval < 0
    ):
        print(
            "--send-width、--timeout、--cost-cap 必须为正数；"
            "--dispatch-interval 不得为负数",
            file=sys.stderr,
        )
        return 2
    try:
        configuration = load_config()
        effective = resolve_llm_profile(configuration.llm, arguments.profile)
        profile = configuration.llm.profiles.get(arguments.profile)
        if profile is None:
            raise ObservationReplayError(f"模型档位不存在：{arguments.profile}")
        if (
            profile.input_price_per_million_usd is None
            or profile.output_price_per_million_usd is None
        ):
            raise ObservationReplayError(f"模型档位 {arguments.profile} 未配置输入/输出单价")
        if not effective.model.strip():
            raise ObservationReplayError(f"模型档位 {arguments.profile} 未配置型号")
        if not os.environ.get(effective.api_key_env, "").strip():
            raise ObservationReplayError(
                f"模型档位 {arguments.profile} 缺少环境变量 {effective.api_key_env}"
            )
        generic = configuration.games.get("generic", AdapterConfig()).generic
        title_map = WindowTitleMap.load()
        prepared = tuple(
            _prepare_replay(
                session,
                arguments.send_width,
                arguments.segment,
                title_map,
                generic.region_sparsity_max,
            )
            for session in arguments.session
        )
        calls = sum(len(item.selected) for item in prepared)
        estimated_cost = calls * (
            ESTIMATED_INPUT_TOKENS_PER_CALL * profile.input_price_per_million_usd
            + effective.max_tokens * profile.output_price_per_million_usd
        ) / 1_000_000.0
        print("即将上传以下由检测器选中的本地 raw 帧：")
        for item in prepared:
            for frame in item.selected:
                print(f"  - {frame.path}")
        print(f"目标档位：{arguments.profile}（型号来自配置：{effective.model}）")
        print(f"预计调用：{calls}；估算输入 {ESTIMATED_INPUT_TOKENS_PER_CALL} token/次")
        print(f"预计花费：${estimated_cost:.6f}；花费上限：${arguments.cost_cap:.6f}")
        for item in prepared:
            print(
                f"{item.name}: {item.frame_count} 帧 -> {len(item.selected)} 次上传；"
                f"请求宽 {arguments.send_width}，raw 宽 {item.source_width}，实际宽 {item.actual_send_width}"
            )
        if estimated_cost > arguments.cost_cap:
            raise ObservationReplayError(
                f"预计花费 ${estimated_cost:.6f} 超过 --cost-cap ${arguments.cost_cap:.6f}"
            )
        if not arguments.yes and input("确认上传以上文件？请输入 YES 继续：").strip() != "YES":
            print("未确认，未上传任何文件。")
            return 0
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        output_root = arguments.output_root / f"observation-replay-{stamp}"
        output_root.mkdir(parents=True, exist_ok=False)
        stopped_by_cost = False
        running_cost = 0.0
        for item in prepared:
            stopped_by_cost = asyncio.run(
                _run_prepared(
                    item,
                    output_root / item.name,
                    llm_configuration=configuration.llm,
                    profile=arguments.profile,
                    context_lines=arguments.context_lines,
                    max_inflight=arguments.max_inflight,
                    timeout=arguments.timeout,
                    region_sparsity_max=generic.region_sparsity_max,
                    cost_cap=arguments.cost_cap,
                    prior_cost=running_cost,
                    dispatch_interval=arguments.dispatch_interval,
                )
            ) or stopped_by_cost
            session_summary = json.loads(
                (output_root / item.name / "session.json").read_text(encoding="utf-8")
            )
            running_cost += float(session_summary["total_cost_usd"])
            if stopped_by_cost:
                break
        after_hashes = {str(item.session): _recording_hash(item.session) for item in prepared}
        hash_matches = {
            str(item.session): after_hashes[str(item.session)] == item.recording_hash
            for item in prepared
        }
        _write_review(output_root, prepared)
        actual_cost = sum(
            float(
                json.loads(
                    (output_root / item.name / "session.json").read_text(
                        encoding="utf-8"
                    )
                )["total_cost_usd"]
            )
            for item in prepared
            if (output_root / item.name / "session.json").is_file()
        )
        run = {
            "started_output_timestamp": stamp,
            "profile": arguments.profile,
            "model": effective.model,
            "parameters": {
                "context_lines": arguments.context_lines,
                "max_inflight": arguments.max_inflight,
                "requested_send_width": arguments.send_width,
                "production_default_send_width": PRODUCTION_SEND_WIDTH,
                "timeout_seconds": arguments.timeout,
                "dispatch_interval_seconds": arguments.dispatch_interval,
                "region_sparsity_max": generic.region_sparsity_max,
                "cost_cap_usd": arguments.cost_cap,
            },
            "price": {
                "input_per_million_usd": profile.input_price_per_million_usd,
                "output_per_million_usd": profile.output_price_per_million_usd,
            },
            "estimated_cost_usd": estimated_cost,
            "actual_cost_usd": actual_cost,
            "stopped_by_cost_guard": stopped_by_cost,
            "sessions": [
                {
                    "path": str(item.session),
                    "frame_count": item.frame_count,
                    "selected_count": len(item.selected),
                    "source_dimensions": [item.source_width, item.source_height],
                    "actual_send_width": item.actual_send_width,
                    "width_difference_note": (
                        f"raw 原图宽 {item.source_width}，实际上传 {item.actual_send_width}；"
                        f"生产默认 {PRODUCTION_SEND_WIDTH}，本次未放大。"
                    ),
                    "recording_hash_before": item.recording_hash,
                    "recording_hash_after": after_hashes[str(item.session)],
                    "recording_hash_matches": hash_matches[str(item.session)],
                }
                for item in prepared
            ],
        }
        (output_root / "run.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"输出：{output_root}")
        print(f"实际花费：${actual_cost:.9f}")
        print(f"录像只读哈希一致：{hash_matches}")
        return 0
    except (ObservationReplayError, OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"离线观察重放未执行：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
