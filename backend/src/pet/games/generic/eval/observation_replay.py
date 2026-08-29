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
import re
import statistics
import sys
import tomllib

from PIL import Image

from pet.core.belief import (
    EvidenceStore,
    FastObservationPayload,
    FrameMetricsPayload,
    KeyWindowPayload,
)
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
from pet.core.input_telemetry import ActionInputTimeline, load_action_input_csv
from pet.games.generic.adapter import GenericVisionAdapter, WindowTitleMap, _focus_geometry

BACKEND_DIRECTORY = Path(__file__).resolve().parents[5]
DEFAULT_OUTPUT_ROOT = BACKEND_DIRECTORY / "eval-reports"
DEFAULT_SEGMENTS_PATH = BACKEND_DIRECTORY / "audit" / "m5-t6-segments.toml"
PRODUCTION_SEND_WIDTH = 896
DEFAULT_COST_CAP_USD = 3.0
ESTIMATED_INPUT_TOKENS_PER_CALL = 4_000
GRID_REFERENCE_PATTERN = re.compile(r"(?i)r\d+c\d+")
TIMESTAMP_PATTERN = re.compile(
    r"(?<!\d)(?:\d{4}-\d{2}-\d{2}[T ]?)?"
    r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?(?!\d)"
)
DATE_PATTERN = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
STRICT_ONLY_PATTERN = re.compile(r"仅[\u3400-\u4dbf\u4e00-\u9fff]{3,5}\Z")
RETROSPECTIVE_TERMS = ("出现了", "较上一帧", "比之前", "此前", "原本")
# This fixed list detects purpose or intent language, not direct input/scene facts
# such as a turning view or repeated clicking. It is intentionally conservative.
PURPOSE_INFERENCE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"表明玩家|说明玩家",
        r"(?:玩家|操作者).{0,8}(?:似乎|可能|试图|想要|打算|意图|准备)",
        r"(?:似乎|可能|试图|想要|打算|意图|准备)(?:在|要)?"
        r"(?:浏览|寻找|选择|查看|切换|确认|测试|退出|进入|攻击|躲避|前往)",
        r"正在(?:浏览|寻找|选择|查看|切换|确认|测试)"
        r"(?:界面|列表|菜单|选项|模式|物品|目标|路线)",
        r"为了|以便|从而",
    )
)


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
    confirmed_region: tuple[str, ...]
    change_ratio: float
    global_change: float
    region_intensity: float
    forced: bool
    baseline_monotonic_seconds: float


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
    input_context: ActionInputTimeline
    input_window_start_monotonic: float | None
    input_csv_missing: bool
    input_direction_available: bool


@dataclass(frozen=True, slots=True)
class LocalStatistics:
    total: int
    only_prefix_old: int
    only_compliant: int
    only_expanded: int
    numeric: int
    grid_leaked: int
    average_length: float


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
    parser = argparse.ArgumentParser(description="通用视觉离线快线观察日志重放")
    parser.add_argument("--session", action="append", required=True, type=Path)
    parser.add_argument("--segment", type=_parse_segment)
    parser.add_argument("--profile", required=True)
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
    region_focus_max: float,
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
    selector = AdaptiveFrameSelector(region_sparsity_max=region_focus_max)
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
                ReplayFrameSpec(
                    path,
                    timing,
                    relative,
                    observation.decision.region_grid,
                    observation.decision.confirmed_region_grid,
                    observation.decision.changed_block_ratio,
                    observation.comparisons.vs_baseline.mean_amplitude * 100.0,
                    observation.decision.confirmed_region_intensity * 100.0,
                    observation.decision.forced,
                    observation.decision.baseline_monotonic_seconds,
                )
            )
    if not included_count or source_size is None:
        raise ObservationReplayError(f"指定区间没有帧：{session}")
    actual_width = min(requested_width, source_size[0])
    loaded_input = load_action_input_csv(session)
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
        input_context=loaded_input.timeline,
        input_window_start_monotonic=(origin + segment.start if segment is not None else None),
        input_csv_missing=loaded_input.csv_missing,
        input_direction_available=loaded_input.direction_available,
    )


def _adapter_configuration(
    *,
    profile: str,
    send_width: int,
    timeout: float,
    max_inflight: int,
    region_focus_max: float,
) -> AdapterConfig:
    return AdapterConfig(
        generic=GenericVisionConfig(
            enabled=True,
            send_width=send_width,
            fast_timeout_seconds=timeout,
            max_inflight=max_inflight,
            region_focus_max=region_focus_max,
            llm_profile=profile,
        )
    )


async def _run_prepared(
    prepared: PreparedReplay,
    output_directory: Path,
    *,
    llm_configuration: LlmConfig,
    profile: str,
    max_inflight: int,
    timeout: float,
    region_focus_max: float,
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
            region_focus_max=region_focus_max,
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
        input_context=prepared.input_context,
        input_window_start_monotonic=prepared.input_window_start_monotonic,
        extra_parameters={
            "mode": "offline_replay",
            "source_session": str(prepared.session),
            "source_dimensions": [prepared.source_width, prepared.source_height],
            "requested_send_width": prepared.requested_send_width,
            "actual_send_width": prepared.actual_send_width,
            "production_default_send_width": PRODUCTION_SEND_WIDTH,
            "input_csv_missing": prepared.input_csv_missing,
            "input_direction_available": prepared.input_direction_available,
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
            await adapter.submit_replay_frame(
                frame,
                prepared.game,
                item.region,
                item.baseline_monotonic_seconds,
                confirmed_region=item.confirmed_region,
                change_ratio=item.change_ratio,
                global_change=item.global_change,
                region_intensity=item.region_intensity,
                forced=item.forced,
            )
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
    session = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    origin_value = session.get("origin_monotonic")
    if origin_value is None:
        return []
    origin = float(origin_value)
    grouped: dict[str, dict[str, object]] = {}
    for event in EvidenceStore.read(directory / "evidence.jsonl"):
        if event.root_capture_id is None:
            continue
        grouped.setdefault(event.root_capture_id, {})[event.kind] = event
    rows: list[dict[str, object]] = []
    for root_capture_id in sorted(grouped, key=lambda value: int(value[1:])):
        frame = grouped[root_capture_id]
        if not {"fast_observation", "frame_metrics", "key_window"}.issubset(frame):
            continue
        fast_event = frame["fast_observation"]
        metrics_event = frame["frame_metrics"]
        key_event = frame["key_window"]
        fast = fast_event.payload  # type: ignore[union-attr]
        metrics = metrics_event.payload  # type: ignore[union-attr]
        key = key_event.payload  # type: ignore[union-attr]
        if not isinstance(fast, FastObservationPayload):
            raise ObservationReplayError(f"{root_capture_id} 的 fast payload 类型错误")
        if not isinstance(metrics, FrameMetricsPayload):
            raise ObservationReplayError(f"{root_capture_id} 的 detector payload 类型错误")
        if not isinstance(key, KeyWindowPayload):
            raise ObservationReplayError(f"{root_capture_id} 的 input payload 类型错误")
        rows.append(
            {
                "seq": int(root_capture_id[1:]),
                "frame_ts": origin + fast_event.observed_at,  # type: ignore[union-attr]
                "wall": metrics.wall,
                "game": fast.game,
                "text": fast.text,
                "region": fast_event.scope.cells if fast_event.scope else None,  # type: ignore[union-attr]
                "reason": metrics.reason,
                "change_ratio": round(metrics.change_ratio, 2),
                "global_change": round(metrics.global_change, 1),
                "region_area_ratio": (
                    round(metrics.region_area_ratio)
                    if metrics.region_area_ratio is not None
                    else None
                ),
                "region_intensity": (
                    round(metrics.region_intensity)
                    if metrics.region_intensity is not None
                    else None
                ),
                "input": key.summary,
                "latency_ms": round(fast.latency_ms, 3),
                "ttft_ms": round(fast.ttft_ms, 3) if fast.ttft_ms is not None else None,
                "dropped": fast.drop_reason,
                "user_prompt": fast.user_prompt,
                "speculation": fast.speculation,
                "input_tokens": fast.input_tokens,
                "output_tokens": fast.output_tokens,
                "actual_model": fast.actual_model,
                "actual_provider": fast.actual_provider,
            }
        )
    return rows


def character_similarity(left: str, right: str) -> float:
    """Return a deterministic character-level similarity ratio in 0..1."""
    return difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()


def _extract_local(text: str) -> str | None:
    """Return the first local body, excluding its label and whitespace."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("【局部】"):
            return stripped.removeprefix("【局部】").strip()
    return None


def _extract_scene(text: str) -> str | None:
    """Return the first Scene body, excluding its label and whitespace."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("【画面】"):
            return stripped.removeprefix("【画面】").strip()
    return None


def _extract_speculation(text: str) -> str | None:
    """Return the first optional speculation body."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("【推测】"):
            return stripped.removeprefix("【推测】").strip()
    return None


def _expected_reason(spec: ReplayFrameSpec) -> str:
    if spec.forced and spec.change_ratio == 0.0:
        return "forced"
    if spec.change_ratio <= 0.25:
        return "sparse"
    if spec.change_ratio <= 0.50:
        return "coarse"
    return "large"


def _local_entries(
    rows: Sequence[dict[str, object]],
) -> list[tuple[dict[str, object], str]]:
    entries: list[tuple[dict[str, object], str]] = []
    for row in rows:
        if row.get("dropped") is not None:
            continue
        body = _extract_local(str(row.get("text", "")))
        if body is not None:
            entries.append((row, body))
    return entries


def _local_statistics(
    rows: Sequence[dict[str, object]],
) -> LocalStatistics:
    """Measure local bodies without reading timestamps or renderer fields."""
    bodies = [body for _row, body in _local_entries(rows)]
    if not bodies:
        return LocalStatistics(0, 0, 0, 0, 0, 0, 0.0)
    compact = ["".join(body.split()) for body in bodies]
    only_prefixed = sum(body.startswith("仅") for body in compact)
    only_compliant = sum(STRICT_ONLY_PATTERN.fullmatch(body) is not None for body in compact)
    only_expanded = sum(body.startswith("仅") and len(body) > 6 for body in compact)
    grid_leaked = sum(GRID_REFERENCE_PATTERN.search(body) is not None for body in bodies)
    numeric = 0
    for body in bodies:
        visible = GRID_REFERENCE_PATTERN.sub("", body)
        visible = TIMESTAMP_PATTERN.sub("", visible)
        visible = DATE_PATTERN.sub("", visible)
        numeric += any(character.isdigit() for character in visible)
    average_length = statistics.mean(len("".join(body.split())) for body in bodies)
    return LocalStatistics(
        len(bodies),
        only_prefixed,
        only_compliant,
        only_expanded,
        numeric,
        grid_leaked,
        average_length,
    )


def _testimony_text(row: dict[str, object]) -> str:
    """Return only testimony segments; speculation is deliberately excluded."""
    text = str(row.get("text", ""))
    return "\n".join(
        body for body in (_extract_scene(text), _extract_local(text)) if body is not None
    )


def _input_attribution_violation(row: dict[str, object]) -> bool:
    testimony = _testimony_text(row)
    return any(pattern.search(testimony) is not None for pattern in PURPOSE_INFERENCE_PATTERNS)


def _retrospective_violation(row: dict[str, object]) -> bool:
    return any(term in str(row.get("text", "")) for term in RETROSPECTIVE_TERMS)


def _segment_shape(values: Sequence[str]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    sentence_counts = [
        max(1, len([part for part in re.split(r"[。！？!?]+", value) if part.strip()]))
        for value in values
    ]
    character_counts = [len("".join(value.split())) for value in values]
    return statistics.mean(sentence_counts), statistics.mean(character_counts)


def _echoed_metric_values(row: dict[str, object]) -> tuple[str, ...]:
    text = str(row.get("text", ""))
    values = [f"{float(row['global_change']):.1f}%"]
    if row.get("region_area_ratio") is not None:
        values.extend(
            (
                f"{int(row['region_area_ratio'])}%",
                f"{int(row['region_intensity'])}%",
            )
        )
    return tuple(dict.fromkeys(value for value in values if value in text))


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
        "# 通用视觉离线观察日志复核表",
        "",
        "本文件只列机器输出与机械统计，不评价模型能力。有信息复读只比较成功输出中"
        "【局部】正文：先移除同时满足‘仅开头且全文 4–6 个汉字’的合规纯特效段，"
        "再按画面时间比较相邻的剩余正文；"
        "【画面】不参与。相似度为字符级 SequenceMatcher 重合率，超过 0.6 的相邻对列为"
        "有信息复读候选，需产品负责人判读。",
        "旧‘仅’口径保留为仅判前缀；新口径要求全文匹配 4–6 个汉字，超出 6 字另计"
        "展开。数字占比与平均字数均只取【局部】正文，不读取时间戳或其他日志字段。",
        "输入归因违约只扫描【画面】与【局部】内的目的性推断；直接陈述视角转动或连续"
        "点击不计，【推测】段内推断另行统计。三类违约均列机械命中原文供人工复核。",
        "",
    ]
    for prepared in prepared_items:
        directory = output_root / prepared.name
        rows = _read_rows(directory)
        session = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        latencies = [float(row["latency_ms"]) for row in rows if row.get("dropped") is None]
        ttfts = [
            float(row["ttft_ms"])
            for row in rows
            if row.get("dropped") is None and row.get("ttft_ms") is not None
        ]
        dropped = Counter(str(row["dropped"]) for row in rows if row.get("dropped") is not None)
        successful = [row for row in rows if row.get("dropped") is None]
        focused = [row for row in successful if row.get("region_area_ratio") is not None]
        injected = len(focused)
        failed_region_requests = sum(
            1
            for row in rows
            if row.get("dropped") is not None
            and row.get("region_area_ratio") is not None
        )
        reason_counts = Counter(str(row.get("reason")) for row in rows)
        focused_paired = sum(
            _extract_local(str(row.get("text", ""))) is not None for row in focused
        )
        input_covered = sum(
            "input" in row and isinstance(row.get("input"), str) and bool(str(row["input"]))
            for row in rows
        )
        forbidden_local = sum(
            row.get("region_area_ratio") is None
            and _extract_local(str(row.get("text", ""))) is not None
            for row in successful
        )
        session_reason_counts = session.get("reason_counts")
        local_entries = _local_entries(rows)
        local_stats = _local_statistics(rows)
        informative_entries = [
            (row, body)
            for row, body in local_entries
            if STRICT_ONLY_PATTERN.fullmatch("".join(body.split())) is None
        ]
        scenes = [
            scene
            for row in successful
            if (scene := _extract_scene(str(row.get("text", "")))) is not None
        ]
        local_bodies = [body for _row, body in local_entries]
        scene_sentences, scene_characters = _segment_shape(scenes)
        local_sentences, local_characters = _segment_shape(local_bodies)
        metrics_covered = sum(
            "global_change" in row
            and (
                (row.get("region_area_ratio") is None and row.get("region_intensity") is None)
                or (
                    row.get("region_area_ratio") is not None
                    and row.get("region_intensity") is not None
                )
            )
            for row in rows
        )
        output_grid_leaked = sum(
            GRID_REFERENCE_PATTERN.search(str(row.get("text", ""))) is not None
            for row in successful
        )
        message_grid_leaked = sum(
            GRID_REFERENCE_PATTERN.search(str(row.get("user_prompt", ""))) is not None
            for row in rows
            if row.get("user_prompt")
        )
        metric_echoes = [
            (row, values)
            for row in successful
            if (values := _echoed_metric_values(row))
        ]
        attribution_violations = [
            row for row in successful if _input_attribution_violation(row)
        ]
        retrospective_violations = [
            row for row in successful if _retrospective_violation(row)
        ]
        speculation_rows = [
            row
            for row in successful
            if _extract_speculation(str(row.get("text", ""))) is not None
        ]
        input_token_values = [
            int(row["input_tokens"])
            for row in successful
            if row.get("input_tokens") is not None
        ]
        rate_limited = sum(count for reason, count in dropped.items() if "429" in reason)
        timed_out = dropped.get("timeout", 0)
        truncated = int(session.get("truncated_count", 0))
        average_visible_tokens = session.get("average_visible_output_tokens")
        exact_repeats = sum(
            previous_body == current_body
            for (_previous, previous_body), (_current, current_body) in zip(
                informative_entries, informative_entries[1:]
            )
        )
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
                (
                    f"- TTFT 中位 / P90：{statistics.median(ttfts):.3f} / "
                    f"{(_percentile(ttfts, 0.90) or 0.0):.3f} ms"
                    if ttfts
                    else "- TTFT 中位 / P90：无可测流式首字"
                ),
                f"- 实际花费：${float(session['total_cost_usd']):.9f}",
                f"- 聚焦区域注入次数（成功响应）：{injected}",
                f"- 失败请求中的区域提示次数：{failed_region_requests}",
                f"- reason 分布：{dict(reason_counts)}",
                f"- session reason 计数与 JSONL 一致："
                f"{session_reason_counts == {reason: reason_counts[reason] for reason in ('sparse', 'coarse', 'large', 'forced')}}",
                (
                    f"- 【局部】/聚焦区域配对率：{focused_paired / len(focused):.2%} "
                    f"（{focused_paired}/{len(focused)}）"
                    if focused
                    else "- 【局部】/聚焦区域配对率：无聚焦区域成功帧"
                ),
                f"- input 字段覆盖率：{input_covered / len(rows):.2%} "
                f"（{input_covered}/{len(rows)}）",
                f"- 无聚焦区域帧违规【局部】条数：{forbidden_local}",
                f"- 【局部】段出现次数：{local_stats.total}",
                (
                    f"- 旧“仅”口径（仅判前缀）："
                    f"{local_stats.only_prefix_old / local_stats.total:.2%} "
                    f"（{local_stats.only_prefix_old}/{local_stats.total}）"
                    if local_stats.total
                    else "- 旧“仅”口径（仅判前缀）：无【局部】段"
                ),
                (
                    f"- “仅”严格合规数：{local_stats.only_compliant}；"
                    f"展开数（超长）：{local_stats.only_expanded}；合规率："
                    f"{local_stats.only_compliant / local_stats.only_prefix_old:.2%}"
                    if local_stats.only_prefix_old
                    else "- “仅”严格合规数：0；展开数（超长）：0；合规率：无“仅”段"
                ),
                (
                    f"- 含具体数字的【局部】占比："
                    f"{local_stats.numeric / local_stats.total:.2%} "
                    f"（{local_stats.numeric}/{local_stats.total}）"
                    if local_stats.total
                    else "- 含具体数字的【局部】占比：无【局部】段"
                ),
                f"- 格子泄漏条数（输出全文）：{output_grid_leaked}",
                f"- 格子泄漏条数（消息体）：{message_grid_leaked}",
                f"- 注入指标复述候选：{len(metric_echoes)}",
                f"- 【局部】平均字数：{local_stats.average_length:.3f}",
                f"- 有信息【局部】段数：{len(informative_entries)}",
                f"- 【画面】平均句数 / 字数：{scene_sentences:.3f} / {scene_characters:.3f}",
                f"- 【局部】平均句数 / 字数：{local_sentences:.3f} / {local_characters:.3f}",
                f"- 三指标 JSONL 覆盖率：{metrics_covered / len(rows):.2%} "
                f"（{metrics_covered}/{len(rows)}）",
                f"- 429 / 超时次数：{rate_limited} / {timed_out}",
                (
                    f"- 输入归因违约：{len(attribution_violations)} / {len(successful)} "
                    f"（{len(attribution_violations) / len(successful):.2%}）"
                    if successful
                    else "- 输入归因违约：无成功输出"
                ),
                (
                    f"- 回溯措辞违约：{len(retrospective_violations)} / {len(successful)} "
                    f"（{len(retrospective_violations) / len(successful):.2%}）"
                    if successful
                    else "- 回溯措辞违约：无成功输出"
                ),
                (
                    f"- 机械指标复述：{len(metric_echoes)} / {len(successful)} "
                    f"（{len(metric_echoes) / len(successful):.2%}）"
                    if successful
                    else "- 机械指标复述：无成功输出"
                ),
                (
                    f"- 【推测】段：{len(speculation_rows)} / {len(successful)} "
                    f"（{len(speculation_rows) / len(successful):.2%}）"
                    if successful
                    else "- 【推测】段：无成功输出"
                ),
                (
                    f"- 平均输入 token：{statistics.mean(input_token_values):.3f}"
                    if input_token_values
                    else "- 平均输入 token：历史日志未记录"
                ),
                f"- 截断条数：{truncated}",
                f"- 有信息逐字重复条数：{exact_repeats}",
                f"- 平均可见输出 token：{average_visible_tokens}",
                (
                    "- 输入 CSV：缺失，所有窗口按无输入处理并已标注。"
                    if prepared.input_csv_missing
                    else "- 输入 CSV：存在，读取时已按生产动作键白名单过滤。"
                ),
                (
                    "- 鼠标方向：录制保留正负方向。"
                    if prepared.input_direction_available
                    else "- 鼠标方向：旧录制仅有横纵绝对量，只报告主要轴，不猜测正负。"
                ),
                f"- 上传宽度：请求 {prepared.requested_send_width}，raw {prepared.source_width}，"
                f"实际 {prepared.actual_send_width}；未放大，低于生产默认。",
                "",
                "### observations.md 新格式前 10 条",
                "",
            ]
        )
        markdown_blocks = (directory / "observations.md").read_text(
            encoding="utf-8"
        ).strip().split("\n\n")
        lines.extend(markdown_blocks[2:12])
        lines.extend(
            [
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
        lines.extend(["", "### 有信息【局部】复读候选（相邻相似度 > 0.6）", ""])
        repeats = []
        for (previous, previous_body), (current, current_body) in zip(
            informative_entries, informative_entries[1:]
        ):
            similarity = character_similarity(previous_body, current_body)
            if similarity > 0.6:
                repeats.append((previous, current, previous_body, current_body, similarity))
        if repeats:
            lines.extend(
                [
                    "| 前一帧时间 | 当前帧时间 | 相似度 | 前文 | 后文 |",
                    "|---|---|---:|---|---|",
                ]
            )
            for previous, current, previous_body, current_body, similarity in repeats:
                left = previous_body.replace("|", "\\|")
                right = current_body.replace("|", "\\|")
                lines.append(
                    f"| {previous['frame_ts']} | {current['frame_ts']} | "
                    f"{similarity:.3f} | {left} | {right} |"
                )
        else:
            lines.append("无。")
        for heading, violations in (
            ("输入归因违约命中原文", attribution_violations),
            ("回溯措辞违约命中原文", retrospective_violations),
        ):
            lines.extend(["", f"### {heading}（最多 20 条）", ""])
            if violations:
                for row in violations[:20]:
                    lines.append(f"- {row['frame_ts']}：{row['text']}")
            else:
                lines.append("无。")
        lines.extend(["", "### 注入指标复述候选", ""])
        if metric_echoes:
            for row, values in metric_echoes[:20]:
                lines.append(
                    f"- {row['frame_ts']}（{', '.join(values)}）：{row['text']}"
                )
        else:
            lines.append("无。")
        specs_by_ts = {
            item.timing.monotonic_seconds: item for item in prepared.selected
        }
        lines.extend(["", "### 新字段与 selector 指标抽样比对", ""])
        metadata_samples: list[tuple[dict[str, object], ReplayFrameSpec]] = []
        for reason in ("sparse", "coarse", "large", "forced"):
            row = next((item for item in rows if item.get("reason") == reason), None)
            if row is None:
                continue
            spec = specs_by_ts.get(float(row["frame_ts"]))
            if spec is not None:
                metadata_samples.append((row, spec))
        if metadata_samples:
            lines.extend(
                [
                    "| 帧单调秒 | 全局变化 日志/selector | 区域占比 日志/selector | 区域强度 日志/selector | 一致 |",
                    "|---:|---|---|---|---|",
                ]
            )
            for row, spec in metadata_samples:
                expected_reason = _expected_reason(spec)
                expected_area = (
                    _focus_geometry(spec.confirmed_region)[1]
                    if expected_reason != "forced" and spec.region
                    else None
                )
                logged_area = row.get("region_area_ratio")
                logged_intensity = row.get("region_intensity")
                expected_intensity = spec.region_intensity if expected_area is not None else None
                matches = (
                    row.get("reason") == expected_reason
                    and float(row["global_change"]) == round(spec.global_change, 1)
                    and logged_area == (round(expected_area) if expected_area is not None else None)
                    and logged_intensity == (
                        round(expected_intensity) if expected_intensity is not None else None
                    )
                )
                lines.append(
                    f"| {float(row['frame_ts']):.6f} | {row.get('global_change')} / "
                    f"{spec.global_change:.6f} | {logged_area} / {expected_area} | "
                    f"{logged_intensity} / {expected_intensity} | {matches} |"
                )
        prompt_rows = [row for row in successful if row.get("user_prompt")]
        sampled: list[dict[str, object]] = []
        for predicate in (
            lambda row: row.get("region_area_ratio") is not None,
            lambda row: row.get("region_area_ratio") is None and row.get("reason") != "forced",
            lambda row: row.get("reason") == "forced",
            lambda row: row.get("input") != "无输入",
        ):
            match = next((row for row in prompt_rows if predicate(row)), None)
            if match is not None and match not in sampled:
                sampled.append(match)
        lines.extend(["", "### 抽样实际用户消息", ""])
        if sampled:
            for row in sampled:
                lines.extend(
                    [
                        f"帧 {row['frame_ts']}：",
                        "",
                        "```text",
                        str(row["user_prompt"]),
                        "```",
                        "",
                    ]
                )
        else:
            lines.append("无可抽样的成功请求。")
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
    lines.extend(
        [
            "## observation-fast.md 全文",
            "",
            "```text",
            (BACKEND_DIRECTORY / "prompts" / "generic" / "observation-fast.md")
            .read_text(encoding="utf-8")
            .rstrip(),
            "```",
            "",
        ]
    )
    (output_root / "review.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console()
    arguments = build_parser().parse_args(argv)
    if arguments.max_inflight < 1:
        print("--max-inflight 必须至少为 1", file=sys.stderr)
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
        configuration = load_config(strict=True)
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
                generic.region_focus_max,
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
                    max_inflight=arguments.max_inflight,
                    timeout=arguments.timeout,
                    region_focus_max=generic.region_focus_max,
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
                "max_inflight": arguments.max_inflight,
                "input_context": True,
                "requested_send_width": arguments.send_width,
                "production_default_send_width": PRODUCTION_SEND_WIDTH,
                "timeout_seconds": arguments.timeout,
                "dispatch_interval_seconds": arguments.dispatch_interval,
                "region_focus_max": generic.region_focus_max,
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
                    "input_csv_missing": item.input_csv_missing,
                    "input_direction_available": item.input_direction_available,
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
