"""Offline grid search for the adaptive frame selector.

The calibration objective is intentionally ordered: missed upload-worthy
frames matter first, upload rate second. Sending one extra image is cheap;
an omitted moment cannot be recovered after the raw stream is gone.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import statistics
import time
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np
from PIL import Image

from pet.core.capture import (
    DECISION_REASONS,
    DEFAULT_INPUT_MOTION_THRESHOLD,
    DEFAULT_MAX_SILENCE_SECONDS,
    DEFAULT_MIN_SAVE_INTERVAL_SECONDS,
    DEFAULT_REGION_SPARSITY_MAX,
    DEFAULT_STRONG_BLOCK_DELTA,
    AdaptiveFrameSelector,
    CaptureError,
    DecisionReason,
    FrameChangeDetector,
    PreparedFrame,
    _load_replay_frame_times,
    _raw_frame_timestamp,
    _replay_session_monotonic_origin,
)

DEFAULT_NOISE_WINDOWS = (10, 20, 40)
DEFAULT_NOISE_MULTIPLIERS = (0.8, 1.0, 1.2, 1.5, 2.0, 2.5)
DEFAULT_NOISE_MARGIN_PIXELS = (0.0, 1.0, 2.0, 4.0, 6.0, 8.0)
# M5-T4/T5a fixed P=1 after P=2 increased both miss proxies sharply.
# It is deliberately not a search dimension in M5-T6.
DEFAULT_PERSISTENCE_VALUES = (1,)
TOP_COMBINATION_COUNT = 20
IDLE_ROLES = (
    "B-idle",
    "C-idle",
    "A-static-1",
    "A-static-2",
    "A-static-3",
    "A-static-4",
    "A-static-5",
)
A_ROLES = (
    "A-static-1",
    "A-static-2",
    "A-static-3",
    "A-static-4",
    "A-static-5",
    "A-world",
    "A-trans-1",
    "A-trans-2",
    "A-trans-3",
    "A-trans-4",
)
TRANSITION_ROLES = ("A-trans-1", "A-trans-2", "A-trans-3", "A-trans-4")
DIRECT_MISS_ROLES = ("text-heavy", *A_ROLES)
INPUT_MISS_ROLES = (*TRANSITION_ROLES, "combat-3a", "explore-3a")


@dataclass(frozen=True, slots=True)
class CalibrationOptions:
    """Inputs for one deterministic calibration run."""

    session_specs: tuple[str, ...]
    output_dir: Path
    segments_path: Path | None = None
    grid_path: Path | None = None
    sample_stride: int = 1
    strong_block_delta: float = DEFAULT_STRONG_BLOCK_DELTA
    input_motion_threshold: float = DEFAULT_INPUT_MOTION_THRESHOLD


@dataclass(frozen=True, slots=True)
class CalibrationParameters:
    """One point in the four-dimensional selector search space."""

    noise_window: int
    noise_multiplier: float
    noise_margin: float
    persistence_polls: int

    @property
    def noise_margin_pixels(self) -> float:
        return self.noise_margin * 255.0

    @property
    def identifier(self) -> str:
        return (
            f"n{self.noise_window}-k{self.noise_multiplier:g}-"
            f"m{self.noise_margin_pixels:g}-p{self.persistence_polls}"
        )


@dataclass(frozen=True, slots=True)
class CalibrationGrid:
    """Validated values used to construct a full Cartesian product."""

    noise_windows: tuple[int, ...]
    noise_multipliers: tuple[float, ...]
    noise_margin_pixels: tuple[float, ...]
    persistence_values: tuple[int, ...]

    def combinations(self) -> tuple[CalibrationParameters, ...]:
        return tuple(
            CalibrationParameters(
                noise_window=noise_window,
                noise_multiplier=noise_multiplier,
                noise_margin=noise_margin_pixels / 255.0,
                persistence_polls=persistence,
            )
            for (
                noise_window,
                noise_multiplier,
                noise_margin_pixels,
                persistence,
            ) in itertools.product(
                self.noise_windows,
                self.noise_multipliers,
                self.noise_margin_pixels,
                self.persistence_values,
            )
        )


@dataclass(frozen=True, slots=True)
class InputEvent:
    """One privacy-filtered row loaded from a session input.csv."""

    timestamp: datetime
    event_type: str
    dx: int
    dy: int
    monotonic_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class SegmentSpec:
    """One read-only half-open interval on a retained recording."""

    role: str
    session_dir: Path
    start_seconds: float | None = None
    end_seconds: float | None = None

    def contains(self, relative_seconds: float) -> bool:
        start = 0.0 if self.start_seconds is None else self.start_seconds
        return relative_seconds >= start and (
            self.end_seconds is None or relative_seconds < self.end_seconds
        )


@dataclass(frozen=True, slots=True)
class PreparedCalibrationSession:
    """Read-only cached raw stream and independently derived truth proxies."""

    role: str
    session_dir: Path
    recorded_label: str
    paths: tuple[Path, ...]
    timestamps: tuple[datetime, ...]
    monotonic_seconds: tuple[float, ...]
    timeline_source: str
    frames: tuple[PreparedFrame, ...]
    strong_adjacent_changes: tuple[bool, ...]
    input_available: bool
    input_activity: tuple[bool, ...] | None
    input_motion: tuple[float, ...] | None
    original_raw_count: int
    sample_stride: int
    strong_block_delta: float
    input_motion_threshold: float
    segment_start_seconds: float | None = None
    segment_end_seconds: float | None = None
    relative_seconds: tuple[float, ...] = ()
    in_scope: tuple[bool, ...] = ()

    @property
    def duration_seconds(self) -> float:
        scoped = [
            value
            for index, value in enumerate(self.monotonic_seconds)
            if not self.in_scope or self.in_scope[index]
        ]
        if len(scoped) < 2:
            return 0.0
        return scoped[-1] - scoped[0]

    @property
    def frame_count(self) -> int:
        return len(self.frames) if not self.in_scope else sum(self.in_scope)


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """All required metrics for one parameter/session replay."""

    combination_index: int
    parameters: CalibrationParameters
    session_role: str
    session_directory: str
    frame_count: int
    duration_seconds: float
    input_available: bool
    sample_stride: int
    strong_block_delta: float
    input_motion_threshold: float
    saved_count: int
    upload_rate: float
    reason_counts: tuple[tuple[DecisionReason, int], ...]
    local_strong_misses: int
    input_activity_misses: int | None
    longest_silence_seconds: float
    average_region_ratio: float | None
    saved_relative_times: tuple[float, ...]
    segment_start_seconds: float | None = None
    segment_end_seconds: float | None = None

    def reason_count(self, reason: DecisionReason) -> int:
        return dict(self.reason_counts)[reason]


def default_grid() -> CalibrationGrid:
    return CalibrationGrid(
        noise_windows=DEFAULT_NOISE_WINDOWS,
        noise_multipliers=DEFAULT_NOISE_MULTIPLIERS,
        noise_margin_pixels=DEFAULT_NOISE_MARGIN_PIXELS,
        persistence_values=DEFAULT_PERSISTENCE_VALUES,
    )


def load_grid(path: Path | None) -> CalibrationGrid:
    """Load the optional TOML override without shrinking it implicitly."""
    if path is None:
        return default_grid()
    try:
        with path.open("rb") as source:
            payload = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CaptureError(f"无法读取校准网格 {path}：{error}") from error
    grid = payload.get("grid")
    if not isinstance(grid, dict):
        raise CaptureError("校准网格 TOML 必须包含 [grid]")
    raw_persistence = grid.get("persistence_polls", [1])
    if raw_persistence != [1]:
        raise CaptureError("M5-T6 的 persistence_polls 固定为 [1]，不得作为网格维度")
    return CalibrationGrid(
        noise_windows=_integer_values(grid, "noise_window", minimum=1),
        noise_multipliers=_float_values(grid, "noise_multiplier", minimum=0.0),
        noise_margin_pixels=_float_values(
            grid, "noise_margin_pixels", minimum=0.0, maximum=255.0
        ),
        persistence_values=(1,),
    )


def _integer_values(
    grid: dict[str, object], key: str, *, minimum: int
) -> tuple[int, ...]:
    raw = grid.get(key)
    if not isinstance(raw, list) or not raw:
        raise CaptureError(f"校准网格 grid.{key} 必须是非空整数数组")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in raw):
        raise CaptureError(f"校准网格 grid.{key} 必须只含整数")
    values = tuple(dict.fromkeys(int(value) for value in raw))
    if any(value < minimum for value in values):
        raise CaptureError(f"校准网格 grid.{key} 的值必须 >= {minimum}")
    return values


def _float_values(
    grid: dict[str, object],
    key: str,
    *,
    minimum: float,
    maximum: float | None = None,
) -> tuple[float, ...]:
    raw = grid.get(key)
    if not isinstance(raw, list) or not raw:
        raise CaptureError(f"校准网格 grid.{key} 必须是非空数字数组")
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw
    ):
        raise CaptureError(f"校准网格 grid.{key} 必须只含数字")
    values = tuple(dict.fromkeys(float(value) for value in raw))
    if any(value < minimum for value in values):
        raise CaptureError(f"校准网格 grid.{key} 的值必须 >= {minimum:g}")
    if maximum is not None and any(value > maximum for value in values):
        raise CaptureError(f"校准网格 grid.{key} 的值必须 <= {maximum:g}")
    return values


def parse_session_spec(spec: str) -> tuple[str, Path]:
    """Accept either a plain directory or an explicit ``role=directory``."""
    if "=" in spec:
        role, raw_path = spec.split("=", maxsplit=1)
        role = role.strip()
        if not role or not raw_path.strip():
            raise CaptureError(f"会话参数格式无效：{spec!r}")
        return role, Path(raw_path)
    path = Path(spec)
    return path.name, path


def load_segments(path: Path) -> tuple[SegmentSpec, ...]:
    """Load role/session/[start,end) entries without touching recordings."""
    try:
        with path.open("rb") as source:
            payload = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CaptureError(f"无法读取区间清单 {path}：{error}") from error
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise CaptureError("区间 TOML 必须包含至少一个 [[segments]]")
    resolved: list[SegmentSpec] = []
    for index, raw in enumerate(raw_segments, start=1):
        if not isinstance(raw, dict):
            raise CaptureError(f"区间 #{index} 必须是 TOML 表")
        role = raw.get("role")
        session = raw.get("session")
        if not isinstance(role, str) or not role.strip():
            raise CaptureError(f"区间 #{index} 的 role 必须是非空字符串")
        if not isinstance(session, str) or not session.strip():
            raise CaptureError(f"区间 {role!r} 的 session 必须是非空字符串")
        start = _optional_segment_number(raw, "start", role)
        end = _optional_segment_number(raw, "end", role)
        if start is not None and start < 0.0:
            raise CaptureError(f"区间 {role!r} 的 start 不能为负数")
        if end is not None and end <= (0.0 if start is None else start):
            raise CaptureError(f"区间 {role!r} 必须满足 end > start")
        session_path = Path(session)
        if not session_path.is_absolute():
            session_path = (path.parent / session_path).resolve()
        resolved.append(
            SegmentSpec(role.strip(), session_path, start, end)
        )
    roles = [segment.role for segment in resolved]
    if len(set(roles)) != len(roles):
        raise CaptureError("区间角色名必须唯一")
    return tuple(resolved)


def _optional_segment_number(
    raw: dict[str, object], key: str, role: str
) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CaptureError(f"区间 {role!r} 的 {key} 必须是数字")
    number = float(value)
    if not math.isfinite(number):
        raise CaptureError(f"区间 {role!r} 的 {key} 必须是有限数字")
    return number


def calibration_segment_specs(options: CalibrationOptions) -> tuple[SegmentSpec, ...]:
    """Combine explicit whole sessions with the optional interval manifest."""
    specs = [
        SegmentSpec(role, session_dir)
        for role, session_dir in map(parse_session_spec, options.session_specs)
    ]
    if options.segments_path is not None:
        specs.extend(load_segments(options.segments_path))
    if not specs:
        raise CaptureError("--calibrate 至少需要一个会话目录或 --segments")
    if len({spec.role for spec in specs}) != len(specs):
        raise CaptureError("校准会话/区间角色必须唯一")
    return tuple(specs)


def prepare_session(
    role: str,
    session_dir: Path,
    *,
    sample_stride: int,
    strong_block_delta: float,
    input_motion_threshold: float,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> PreparedCalibrationSession:
    """Decode one immutable recording and precompute parameter-independent facts."""
    if sample_stride <= 0:
        raise ValueError("sample_stride must be positive")
    raw_dir = session_dir / "raw"
    all_paths = sorted(raw_dir.glob("raw-*.jpg"))
    if not all_paths:
        raise CaptureError(f"会话没有可校准的 raw JPEG：{raw_dir}")
    all_frame_times = _load_replay_frame_times(raw_dir, all_paths)
    session_origin = _replay_session_monotonic_origin(raw_dir, all_frame_times)
    segment = SegmentSpec(role, session_dir, start_seconds, end_seconds)
    context = [
        (path, frame_time)
        for path, frame_time in zip(all_paths, all_frame_times, strict=True)
        if end_seconds is None
        or frame_time.monotonic_seconds - session_origin < end_seconds
    ]
    if not any(
        segment.contains(frame_time.monotonic_seconds - session_origin)
        for _, frame_time in context
    ):
        raise CaptureError(
            f"区间 {role!r} 没有 raw 帧：{start_seconds!r}..{end_seconds!r}"
        )
    sampled = context[::sample_stride]
    sampled_paths = tuple(item[0] for item in sampled)
    frame_times = tuple(item[1] for item in sampled)
    detector = FrameChangeDetector()
    timestamps: list[datetime] = []
    frames: list[PreparedFrame] = []
    for path in sampled_paths:
        timestamps.append(_raw_frame_timestamp(path))
        try:
            with Image.open(path) as source:
                frames.append(detector.prepare(source.convert("RGB")))
        except OSError as error:
            raise CaptureError(f"无法读取 raw 帧 {path}：{error}") from error
    strong_changes = [False]
    for previous, current in zip(frames, frames[1:]):
        differences = detector.block_mean_differences(previous, current)
        strong_changes.append(bool(np.any(differences > strong_block_delta)))

    input_path = session_dir / "input.csv"
    if input_path.is_file():
        events, input_has_monotonic = _load_input_events(input_path)
        frame_has_monotonic = all(item.recorded_monotonic for item in frame_times)
        use_monotonic = input_has_monotonic and frame_has_monotonic
        if frame_has_monotonic and not input_has_monotonic:
            print(
                f"旧会话 {session_dir.name} 的 input.csv 缺少单调秒；"
                "跨流对齐整段回退 UTC 墙钟（仅标注一次）"
            )
        segment_events = _events_in_segment(
            events,
            session_origin=session_origin,
            start_seconds=None,
            end_seconds=end_seconds,
            use_monotonic=use_monotonic,
            wall_origin=all_frame_times[0].wall_time.timestamp(),
        )
        input_activity, input_motion = _align_input_events(
            segment_events,
            tuple(timestamps),
            tuple(item.monotonic_seconds for item in frame_times),
            use_monotonic=use_monotonic,
            input_motion_threshold=input_motion_threshold,
        )
        input_available = True
        timeline_source = "monotonic" if use_monotonic else "wall-clock-fallback"
    else:
        input_activity = None
        input_motion = None
        input_available = False
        timeline_source = (
            "monotonic"
            if all(item.recorded_monotonic for item in frame_times)
            else "wall-clock-fallback"
        )

    recorded_label = _recorded_label(session_dir)
    return PreparedCalibrationSession(
        role=role,
        session_dir=session_dir,
        recorded_label=recorded_label,
        paths=sampled_paths,
        timestamps=tuple(timestamps),
        monotonic_seconds=(
            tuple(item.monotonic_seconds for item in frame_times)
            if timeline_source == "monotonic"
            else tuple(timestamp.timestamp() for timestamp in timestamps)
        ),
        timeline_source=timeline_source,
        frames=tuple(frames),
        strong_adjacent_changes=tuple(strong_changes),
        input_available=input_available,
        input_activity=input_activity,
        input_motion=input_motion,
        original_raw_count=len(all_paths),
        sample_stride=sample_stride,
        strong_block_delta=strong_block_delta,
        input_motion_threshold=input_motion_threshold,
        segment_start_seconds=start_seconds,
        segment_end_seconds=end_seconds,
        relative_seconds=tuple(
            item.monotonic_seconds - session_origin for item in frame_times
        ),
        in_scope=tuple(
            segment.contains(item.monotonic_seconds - session_origin)
            for item in frame_times
        ),
    )


def _events_in_segment(
    events: Sequence[InputEvent],
    *,
    session_origin: float,
    start_seconds: float | None,
    end_seconds: float | None,
    use_monotonic: bool,
    wall_origin: float,
) -> tuple[InputEvent, ...]:
    start = 0.0 if start_seconds is None else start_seconds
    selected: list[InputEvent] = []
    for event in events:
        event_time = (
            event.monotonic_seconds
            if use_monotonic
            else event.timestamp.timestamp()
        )
        if event_time is None:
            continue
        origin = session_origin if use_monotonic else wall_origin
        relative = event_time - origin
        if relative >= start and (end_seconds is None or relative < end_seconds):
            selected.append(event)
    return tuple(selected)


def _recorded_label(session_dir: Path) -> str:
    path = session_dir / "session.json"
    if not path.is_file():
        return "（无 session.json）"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "（session.json 无法读取）"
    label = payload.get("标签")
    return label if isinstance(label, str) and label else "（未标注）"


def _load_input_events(path: Path) -> tuple[tuple[InputEvent, ...], bool]:
    events: list[InputEvent] = []
    has_monotonic = False
    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            has_monotonic = bool(
                reader.fieldnames and "单调秒" in reader.fieldnames
            )
            for row_number, row in enumerate(reader, start=2):
                try:
                    timestamp = datetime.fromisoformat(row["时间"])
                    monotonic_text = row.get("单调秒", "")
                    monotonic_seconds = (
                        float(monotonic_text) if monotonic_text else None
                    )
                    has_monotonic = has_monotonic and monotonic_seconds is not None
                    event_type = row["事件类型"]
                    dx = int(row["dx"] or 0)
                    dy = int(row["dy"] or 0)
                except (KeyError, TypeError, ValueError) as error:
                    raise CaptureError(
                        f"输入遥测格式无效：{path}:{row_number}"
                    ) from error
                events.append(
                    InputEvent(
                        timestamp,
                        event_type,
                        dx,
                        dy,
                        monotonic_seconds,
                    )
                )
    except OSError as error:
        raise CaptureError(f"无法读取输入遥测 {path}：{error}") from error
    events.sort(
        key=lambda event: (
            event.monotonic_seconds
            if has_monotonic and event.monotonic_seconds is not None
            else event.timestamp.timestamp()
        )
    )
    return tuple(events), has_monotonic


def _align_input_events(
    events: Sequence[InputEvent],
    timestamps: tuple[datetime, ...],
    monotonic_seconds: tuple[float, ...],
    *,
    use_monotonic: bool,
    input_motion_threshold: float,
) -> tuple[tuple[bool, ...], tuple[float, ...]]:
    """Aggregate input rows into the same half-open polling windows as frames."""
    activity: list[bool] = []
    motion: list[float] = []
    event_index = 0
    if len(timestamps) != len(monotonic_seconds):
        raise ValueError("frame wall and monotonic timelines must have equal lengths")
    for timestamp, frame_monotonic in zip(
        timestamps, monotonic_seconds, strict=True
    ):
        key_event = False
        motion_total = 0.0
        frame_time = frame_monotonic if use_monotonic else timestamp.timestamp()
        while event_index < len(events):
            event = events[event_index]
            event_time = (
                event.monotonic_seconds
                if use_monotonic
                else event.timestamp.timestamp()
            )
            if event_time is None or event_time > frame_time:
                break
            if event.event_type in {"按下", "抬起"}:
                key_event = True
            elif event.event_type == "移动汇总":
                motion_total += math.hypot(event.dx, event.dy)
            event_index += 1
        activity.append(key_event or motion_total > input_motion_threshold)
        motion.append(motion_total)
    return tuple(activity), tuple(motion)


def evaluate_session(
    session: PreparedCalibrationSession,
    parameters: CalibrationParameters,
    *,
    combination_index: int,
) -> CalibrationResult:
    """Replay the exact selector state machine and calculate three miss proxies."""
    selector = AdaptiveFrameSelector(
        noise_window=parameters.noise_window,
        noise_multiplier=parameters.noise_multiplier,
        noise_margin=parameters.noise_margin,
        persistence_polls=parameters.persistence_polls,
        region_sparsity_max=DEFAULT_REGION_SPARSITY_MAX,
        min_save_interval=DEFAULT_MIN_SAVE_INTERVAL_SECONDS,
        max_silence=DEFAULT_MAX_SILENCE_SECONDS,
    )
    reason_counts: dict[DecisionReason, int] = {
        reason: 0 for reason in DECISION_REASONS
    }
    saved_count = 0
    local_strong_misses = 0
    input_activity_misses = 0 if session.input_available else None
    current_silence = 0.0
    longest_silence = 0.0
    region_ratios: list[float] = []
    saved_relative_times: list[float] = []
    previous_monotonic: float | None = None
    for index, (frame, monotonic_seconds) in enumerate(
        zip(session.frames, session.monotonic_seconds, strict=True)
    ):
        decision = selector.observe_prepared(frame, monotonic_seconds)
        in_scope = not session.in_scope or session.in_scope[index]
        if not in_scope:
            continue
        reason_counts[decision.reason] += 1
        saved_count += int(decision.should_save)
        if decision.should_save:
            if session.relative_seconds:
                saved_relative_times.append(session.relative_seconds[index])
            else:
                saved_relative_times.append(
                    monotonic_seconds - session.monotonic_seconds[0]
                )
        if decision.region_grid:
            region_ratios.append(decision.changed_block_ratio)
        if decision.reason == "no_change":
            if session.strong_adjacent_changes[index]:
                local_strong_misses += 1
            if (
                input_activity_misses is not None
                and session.input_activity is not None
                and session.input_activity[index]
            ):
                input_activity_misses += 1
            if previous_monotonic is not None:
                current_silence += monotonic_seconds - previous_monotonic
                longest_silence = max(longest_silence, current_silence)
        else:
            current_silence = 0.0
        previous_monotonic = monotonic_seconds
    frame_count = session.frame_count
    return CalibrationResult(
        combination_index=combination_index,
        parameters=parameters,
        session_role=session.role,
        session_directory=str(session.session_dir),
        frame_count=frame_count,
        duration_seconds=session.duration_seconds,
        input_available=session.input_available,
        sample_stride=session.sample_stride,
        strong_block_delta=session.strong_block_delta,
        input_motion_threshold=session.input_motion_threshold,
        saved_count=saved_count,
        upload_rate=saved_count / frame_count,
        reason_counts=tuple((reason, reason_counts[reason]) for reason in DECISION_REASONS),
        local_strong_misses=local_strong_misses,
        input_activity_misses=input_activity_misses,
        longest_silence_seconds=longest_silence,
        average_region_ratio=(
            None if not region_ratios else statistics.mean(region_ratios)
        ),
        saved_relative_times=tuple(saved_relative_times),
        segment_start_seconds=session.segment_start_seconds,
        segment_end_seconds=session.segment_end_seconds,
    )


def _percentile(ordered: list[float], percentile: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _result_bytes(result: CalibrationResult) -> bytes:
    return json.dumps(
        asdict(result),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


GRID_RESULT_HEADER = (
    "参数组合序号",
    "参数组合",
    "noise_window",
    "noise_multiplier",
    "noise_margin",
    "noise_margin_pixels",
    "persistence_polls",
    "会话角色",
    "会话目录",
    "区间起点相对秒",
    "区间终点相对秒",
    "帧数",
    "时长秒",
    "input_csv可用",
    "sample_stride",
    "strong_block_delta",
    "input_motion_threshold",
    "落盘数",
    "上传率",
    "persistent_change次数",
    "persistent_change占比",
    "forced次数",
    "forced占比",
    "suppressed_min_interval次数",
    "suppressed_min_interval占比",
    "no_change次数",
    "no_change占比",
    "局部剧变漏检",
    "输入活动漏检",
    "最长静默秒",
    "region_grid非空平均占比",
    "上传时刻相对秒",
)


def write_grid_results(path: Path, results: Sequence[CalibrationResult]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as destination:
        writer = csv.writer(destination)
        writer.writerow(GRID_RESULT_HEADER)
        for result in results:
            frame_count = result.frame_count
            parameters = result.parameters
            reason_values: list[object] = []
            for reason in DECISION_REASONS:
                count = result.reason_count(reason)
                reason_values.extend((count, _decimal(count / frame_count)))
            writer.writerow(
                (
                    result.combination_index,
                    parameters.identifier,
                    parameters.noise_window,
                    _decimal(parameters.noise_multiplier),
                    _decimal(parameters.noise_margin),
                    _decimal(parameters.noise_margin_pixels),
                    parameters.persistence_polls,
                    result.session_role,
                    result.session_directory,
                    ""
                    if result.segment_start_seconds is None
                    else _decimal(result.segment_start_seconds),
                    ""
                    if result.segment_end_seconds is None
                    else _decimal(result.segment_end_seconds),
                    frame_count,
                    _decimal(result.duration_seconds),
                    "是" if result.input_available else "否",
                    result.sample_stride,
                    _decimal(result.strong_block_delta),
                    _decimal(result.input_motion_threshold),
                    result.saved_count,
                    _decimal(result.upload_rate),
                    *reason_values,
                    result.local_strong_misses,
                    "" if result.input_activity_misses is None else result.input_activity_misses,
                    _decimal(result.longest_silence_seconds),
                    ""
                    if result.average_region_ratio is None
                    else _decimal(result.average_region_ratio),
                    ";".join(_decimal(value) for value in result.saved_relative_times),
                )
            )


def _decimal(value: float) -> str:
    return f"{value:.9f}"


def _group_by_parameters(
    results: Sequence[CalibrationResult],
) -> dict[CalibrationParameters, list[CalibrationResult]]:
    grouped: dict[CalibrationParameters, list[CalibrationResult]] = {}
    for result in results:
        grouped.setdefault(result.parameters, []).append(result)
    return grouped


def _ranked_combinations(
    results: Sequence[CalibrationResult],
) -> list[tuple[CalibrationParameters, int, int, float, int, int]]:
    ranked: list[tuple[CalibrationParameters, int, int, float, int, int]] = []
    for parameters, rows in _group_by_parameters(results).items():
        local_rows = [row for row in rows if row.session_role in DIRECT_MISS_ROLES]
        local_total = sum(row.local_strong_misses for row in local_rows)
        input_rows = [
            row
            for row in rows
            if row.session_role in INPUT_MISS_ROLES
            and row.input_activity_misses is not None
        ]
        input_total = sum(int(row.input_activity_misses) for row in input_rows)
        ranked.append(
            (
                parameters,
                local_total,
                input_total,
                statistics.mean(row.upload_rate for row in rows),
                len(local_rows),
                len(input_rows),
            )
        )
    ranked.sort(key=lambda item: (item[1], item[2], item[3], item[0].identifier))
    return ranked


def _markdown_table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def _format_number(value: float | None, digits: int = 3) -> str:
    return "不可用" if value is None else f"{value:.{digits}f}"


def _marginal_table(
    results: Sequence[CalibrationResult],
    attribute: str,
    display: Callable[[object], str],
) -> str:
    values = sorted({getattr(row.parameters, attribute) for row in results})
    rows: list[tuple[object, ...]] = []
    for value in values:
        selected = [row for row in results if getattr(row.parameters, attribute) == value]
        input_values = [
            int(row.input_activity_misses)
            for row in selected
            if row.input_activity_misses is not None
        ]
        rows.append(
            (
                display(value),
                _format_number(statistics.mean(row.local_strong_misses for row in selected)),
                _format_number(statistics.mean(input_values) if input_values else None),
                _format_number(
                    statistics.mean(row.longest_silence_seconds for row in selected)
                ),
                len(selected),
            )
        )
    return _markdown_table(
        ("参数值", "局部剧变漏检均值", "输入活动漏检均值", "最长静默均值秒", "样本行数"),
        rows,
    )


def _role_row(
    rows: Sequence[CalibrationResult], role: str
) -> CalibrationResult | None:
    return next((row for row in rows if row.session_role == role), None)


def write_summary(
    path: Path,
    *,
    sessions: Sequence[PreparedCalibrationSession],
    results: Sequence[CalibrationResult],
    grid: CalibrationGrid,
    options: CalibrationOptions,
    elapsed_seconds: float,
    deterministic: bool,
    deterministic_hash: str,
) -> None:
    ranked = _ranked_combinations(results)
    grouped = _group_by_parameters(results)
    top = ranked[0] if ranked else None
    boundary_warning = bool(
        top
        and (
            math.isclose(top[0].noise_multiplier, min(grid.noise_multipliers))
            or math.isclose(
                top[0].noise_margin_pixels, min(grid.noise_margin_pixels)
            )
        )
    )
    lines = [
        "# M5-T6 第二轮检测器参数网格数据",
        "",
        "本报告只列离线测量数据，不给出参数选型结论。首要目标是不漏掉应上传的帧，上传率是次要目标。区间边界是 input.csv 机械切分的近似值；角色名不等于逐帧人工画面判定。",
        "",
        "## 口径与运行信息",
        "",
        f"- 参数组合：{len(grid.combinations())} 组",
        f"- 会话/区间角色：{len(sessions)} 个",
        f"- `grid-results.csv` 数据行：{len(results)}（{len(grid.combinations())} × {len(sessions)}）",
        "- P 固定为 1，不参与搜索。",
        f"- `sample_stride`：{options.sample_stride}（抽样率约 {1 / options.sample_stride:.2%}）",
        (
            f"- 局部剧变阈值：{options.strong_block_delta:.9f}"
            f"（{options.strong_block_delta * 255:.3f}/255；初始值待校准）"
        ),
        f"- 输入鼠标位移阈值：{options.input_motion_threshold:.3f} 像素/轮询窗口（初始值待校准）",
        "- 局部剧变漏检：相邻纳入重放的帧中任一 16×9 块平均差严格大于阈值，而本轮理由为 `no_change`。",
        "- 输入活动漏检：轮询窗口内存在按下/抬起，或鼠标相对位移累计严格大于阈值，而本轮理由为 `no_change`。缺少 input.csv 时为不可用。",
        "- 最长静默：连续 `no_change` 轮询覆盖的单调时钟时长。",
        f"- 总运行时长：{elapsed_seconds:.3f} 秒",
        f"- 确定性复核：{'通过' if deterministic else '失败'}；两次结果字节 SHA-256 `{deterministic_hash}`",
        "- B-idle、C-idle 与 A-static-1..5 的上传率分别列出，未求 idle 平均值。",
        "- A-world 只表示产品负责人标注的机械区间；本工具不把区间语义扩展到具体帧。",
        (
            "- **边界警告：最优点可能仍在网格之外。**"
            if boundary_warning
            else "- 边界警告：未触发（排序第一组合未落在 k/margin 下边界）。"
        ),
        "",
        "## 会话基本情况",
        "",
        _markdown_table(
            ("角色", "原录制标签", "目录", "区间 [秒)", "原始帧数", "预热+统计帧", "统计帧数", "时长秒", "input.csv", "时钟"),
            (
                (
                    session.role,
                    session.recorded_label,
                    session.session_dir.name,
                    (
                        f"[{0.0 if session.segment_start_seconds is None else session.segment_start_seconds:g}, "
                        f"{'全段' if session.segment_end_seconds is None else f'{session.segment_end_seconds:g}'})"
                    ),
                    session.original_raw_count,
                    len(session.frames),
                    session.frame_count,
                    f"{session.duration_seconds:.3f}",
                    "可用" if session.input_available else "不可用",
                    session.timeline_source,
                )
                for session in sessions
            ),
        ),
        "",
        "## 三级排序前若干组合",
        "",
        "排序键依次为：(1) text-heavy + 会话 A 全部区间的局部剧变漏检总数；(2) A-trans-1..4 + combat-3a + explore-3a 的输入活动漏检总数；(3) 全体角色平均上传率。idle 上传率与 A-world 命中不参与排序。",
        "",
        _markdown_table(
            ("名次", "参数组合", "局部剧变漏检总数", "局部角色数", "输入活动漏检总数", "输入角色数", "全体平均上传率"),
            (
                (rank, item[0].identifier, item[1], item[4], item[2], item[5], f"{item[3]:.2%}")
                for rank, item in enumerate(ranked[:TOP_COMBINATION_COUNT], start=1)
            ),
        ),
        "",
        "## 单参数边际影响",
        "",
        "口径：固定某参数值后，对所有其他参数组合及全部会话的结果取算术平均；输入指标只纳入 input.csv 可用行。",
        "",
    ]
    marginal_specs = (
        ("噪声窗口 N", "noise_window", lambda value: str(value)),
        ("地板系数 k", "noise_multiplier", lambda value: f"{value:g}"),
        ("余量 margin", "noise_margin", lambda value: f"{value * 255:g}/255"),
    )
    for title, attribute, formatter in marginal_specs:
        lines.extend(
            (
                f"### {title}",
                "",
                _marginal_table(results, attribute, formatter),
                "",
            )
        )
    lines.extend(("## 各 idle 角色上传率（禁止平均）", ""))
    lines.append(
        _markdown_table(
            ("参数组合", *IDLE_ROLES),
            (
                (
                    parameters.identifier,
                    *(
                        "不可用"
                        if _role_row(rows, role) is None
                        else f"{_role_row(rows, role).upload_rate:.2%}"
                        for role in IDLE_ROLES
                    ),
                )
                for parameters, rows in grouped.items()
            ),
        )
    )
    lines.extend(("", "## A-world 上传次数与时刻", ""))
    lines.append(
        _markdown_table(
            ("参数组合", "上传次数", "相对会话起点时刻秒"),
            (
                (
                    parameters.identifier,
                    (
                        "⚠ 0（漏掉该机械区间内的世界事件）"
                        if (world := _role_row(rows, "A-world")) is not None
                        and world.saved_count == 0
                        else ("不可用" if world is None else world.saved_count)
                    ),
                    (
                        "不可用"
                        if world is None
                        else ", ".join(f"{value:.3f}" for value in world.saved_relative_times)
                    ),
                )
                for parameters, rows in grouped.items()
            ),
        )
    )
    lines.extend(("", "## A-trans 输入活动漏检（分别列出）", ""))
    lines.append(
        _markdown_table(
            ("参数组合", *TRANSITION_ROLES),
            (
                (
                    parameters.identifier,
                    *(
                        "不可用"
                        if (row := _role_row(rows, role)) is None
                        or row.input_activity_misses is None
                        else row.input_activity_misses
                        for role in TRANSITION_ROLES
                    ),
                )
                for parameters, rows in grouped.items()
            ),
        )
    )
    lines.extend(("", "## C-idle 上传率 vs text-heavy 局部剧变漏检（108 组散点数据）", ""))
    lines.append(
        _markdown_table(
            ("参数组合", "C-idle 上传率（横轴）", "text-heavy 局部剧变漏检（纵轴）"),
            (
                (
                    parameters.identifier,
                    (
                        "不可用"
                        if (idle := _role_row(rows, "C-idle")) is None
                        else f"{idle.upload_rate:.9f}"
                    ),
                    (
                        "不可用"
                        if (text := _role_row(rows, "text-heavy")) is None
                        else text.local_strong_misses
                    ),
                )
                for parameters, rows in grouped.items()
            ),
        )
    )
    missing = [session.role for session in sessions if not session.input_available]
    lines.extend(("", "## 输入指标不可用会话", ""))
    lines.append("无。" if not missing else "、".join(missing) + "（对应 CSV 单元格留空，不填零）。")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


@dataclass(frozen=True, slots=True)
class CrossCorrelationMeasurement:
    role: str
    direct_lag_seconds: float | None
    correlation_lag_seconds: float | None
    peak_correlation: float | None
    input_event_count: int
    frame_difference_count: int
    note: str


def _correlation_peak_lag(
    input_times: np.ndarray,
    input_values: np.ndarray,
    frame_times: np.ndarray,
    frame_values: np.ndarray,
    *,
    minimum_lag: float = -5.0,
    maximum_lag: float = 15.0,
    lag_step: float = 0.1,
) -> tuple[float, float] | None:
    """Find the positive Pearson peak while respecting the 1 s frame cadence.

    Input is retained at 100 ms resolution.  For each candidate lag, activity is
    summed over a one-second window ending at ``frame_time - lag`` and compared
    with the sparse frame-difference samples.  Reported tenths are search-grid
    coordinates, not sub-frame precision; the source frame cadence remains the
    approximately one-second accuracy limit.
    """
    if len(input_times) == 0 or len(frame_times) < 3:
        return None
    lags = np.arange(minimum_lag, maximum_lag + lag_step / 2.0, lag_step)
    best: tuple[float, float] | None = None
    for lag in lags:
        aligned: list[float] = []
        for frame_time in frame_times:
            target = frame_time - lag
            mask = (input_times > target - 1.0) & (input_times <= target)
            aligned.append(float(np.sum(input_values[mask])))
        input_vector = np.asarray(aligned, dtype=np.float64)
        if np.ptp(input_vector) == 0.0 or np.ptp(frame_values) == 0.0:
            continue
        correlation = float(np.corrcoef(input_vector, frame_values)[0, 1])
        if not math.isfinite(correlation):
            continue
        candidate = (float(round(lag, 10)), correlation)
        if best is None or candidate[1] > best[1]:
            best = candidate
    return best


def measure_crosscorrelation(
    spec: SegmentSpec,
    *,
    strong_block_delta: float,
    negative_control: bool = False,
) -> CrossCorrelationMeasurement:
    """Measure direct and correlation lags without mutating retained data."""
    raw_dir = spec.session_dir / "raw"
    all_paths = sorted(raw_dir.glob("raw-*.jpg"))
    if not all_paths:
        raise CaptureError(f"互相关会话没有 raw JPEG：{raw_dir}")
    frame_times = _load_replay_frame_times(raw_dir, all_paths)
    origin = _replay_session_monotonic_origin(raw_dir, frame_times)
    start = 0.0 if spec.start_seconds is None else spec.start_seconds
    end = (
        frame_times[-1].monotonic_seconds - origin
        if spec.end_seconds is None
        else spec.end_seconds
    )
    if negative_control:
        # Negative controls must stay inside the declared zero-input role.  The
        # B-idle recording intentionally has stop-recording input immediately
        # after its [0, 147.7) role; importing the transition would invalidate
        # the control rather than test the correlation implementation.
        window_start = start
        window_end = end
    else:
        window_start = max(0.0, start - 5.0)
        window_end = end + 15.0
    selected = [
        (path, frame_time, frame_time.monotonic_seconds - origin)
        for path, frame_time in zip(all_paths, frame_times, strict=True)
        if window_start <= frame_time.monotonic_seconds - origin < window_end
    ]
    detector = FrameChangeDetector()
    prepared: list[PreparedFrame] = []
    for path, _, _ in selected:
        try:
            with Image.open(path) as source:
                prepared.append(detector.prepare(source.convert("RGB")))
        except OSError as error:
            raise CaptureError(f"无法读取互相关 raw 帧 {path}：{error}") from error
    difference_times: list[float] = []
    difference_values: list[float] = []
    strong_times: list[float] = []
    for previous, current, selected_item in zip(
        prepared[:-1], prepared[1:], selected[1:], strict=True
    ):
        block_differences = detector.block_mean_differences(previous, current)
        maximum = float(np.max(block_differences))
        relative = selected_item[2]
        difference_times.append(relative)
        difference_values.append(maximum)
        if maximum > strong_block_delta:
            strong_times.append(relative)

    input_path = spec.session_dir / "input.csv"
    if not input_path.is_file():
        return CrossCorrelationMeasurement(
            spec.role, None, None, None, 0, len(difference_times),
            "input.csv 不存在，无法计算",
        )
    events, has_monotonic = _load_input_events(input_path)
    if not has_monotonic or not all(item.recorded_monotonic for item in frame_times):
        return CrossCorrelationMeasurement(
            spec.role, None, None, None, 0, len(difference_times),
            "缺少统一单调时钟；互相关不使用墙钟混算",
        )
    window_events = [
        event
        for event in events
        if event.monotonic_seconds is not None
        and window_start <= event.monotonic_seconds - origin < window_end
        and event.event_type != "焦点丢失"
    ]
    segment_events = [
        event
        for event in window_events
        if event.monotonic_seconds is not None
        and start <= event.monotonic_seconds - origin < end
    ]
    first_input = (
        None
        if not segment_events
        else float(segment_events[0].monotonic_seconds - origin)  # type: ignore[operator]
    )
    direct = (
        None
        if first_input is None or not strong_times
        else strong_times[0] - first_input
    )

    bin_count = max(1, int(math.ceil((window_end - window_start) / 0.1)))
    input_values = np.zeros(bin_count, dtype=np.float64)
    input_times = window_start + (np.arange(bin_count, dtype=np.float64) + 0.5) * 0.1
    for event in window_events:
        relative = float(event.monotonic_seconds - origin)  # type: ignore[operator]
        index = min(bin_count - 1, max(0, int((relative - window_start) / 0.1)))
        if event.event_type in {"按下", "抬起"}:
            input_values[index] += 1.0
        elif event.event_type == "移动汇总":
            input_values[index] += math.hypot(event.dx, event.dy)
    active_mask = input_values > 0.0
    peak = _correlation_peak_lag(
        input_times[active_mask],
        input_values[active_mask],
        np.asarray(difference_times, dtype=np.float64),
        np.asarray(difference_values, dtype=np.float64),
    )
    if peak is None:
        note = (
            "负对照无输入活动，未产生可计算峰"
            if negative_control and not np.any(active_mask)
            else "输入或帧差序列无方差，未产生可计算峰"
        )
        return CrossCorrelationMeasurement(
            spec.role,
            direct,
            None,
            None,
            len(segment_events),
            len(difference_times),
            note,
        )
    return CrossCorrelationMeasurement(
        spec.role,
        direct,
        peak[0],
        peak[1],
        len(segment_events),
        len(difference_times),
        "负对照出现峰，结论作废并需重查" if negative_control else "可计算",
    )


def write_crosscorr(
    path: Path,
    *,
    specs: Sequence[SegmentSpec],
    strong_block_delta: float,
) -> tuple[CrossCorrelationMeasurement, ...]:
    by_role = {spec.role: spec for spec in specs}
    requested = (*TRANSITION_ROLES, "B-idle", "C-idle")
    measurements: list[CrossCorrelationMeasurement] = []
    for role in requested:
        spec = by_role.get(role)
        if spec is None:
            measurements.append(
                CrossCorrelationMeasurement(
                    role, None, None, None, 0, 0, "segments 中没有此角色"
                )
            )
            continue
        measurements.append(
            measure_crosscorrelation(
                spec,
                strong_block_delta=strong_block_delta,
                negative_control=role in {"B-idle", "C-idle"},
            )
        )
    transition = [item for item in measurements if item.role in TRANSITION_ROLES]
    lines = [
        "# M5-T6 输入/画面互相关校准",
        "",
        "本文件只报告测量，不把滞后常数写入生产代码或配置。区间为 input.csv 机械切分的近似值，不代表逐帧人工画面判定。",
        "",
        "## 口径",
        "",
        f"- 直接口径：区间第一个输入事件与扩展窗口内第一个块最大差严格超过 {strong_block_delta * 255:.3f}/255 的帧之差。窗口为 [段起−5 秒, 段止+15 秒]。",
        "- 相关口径：输入按 100 ms 分桶；按下/抬起各计 1，移动汇总计 hypot(dx,dy)。帧信号为相邻 raw 帧 16×9 块平均差的最大值。每个候选 lag 将此前 1 秒输入活动与帧差作 Pearson 相关，搜索 −5.0..+15.0 秒。",
        "- 正 lag 表示画面晚于输入。相关搜索步长虽为 0.1 秒，但 raw 帧约 1 秒一张，**有效分辨率仍约 1 秒**，不得把小数位解释为亚秒精度。",
        "- 轮询量化使输入→画面滞后额外带有约 +0..1 秒的系统性偏差上界。",
        "- A-trans 使用规定的前 5/后 15 秒扩展窗口；B/C 负对照只使用各自声明的零输入区间，避免把 B-idle 区间后的停止录制操作引入负对照。",
        "",
        "## 分段测量",
        "",
        _markdown_table(
            ("角色", "直接滞后秒", "相关峰滞后秒", "峰相关系数", "段内输入事件", "帧差样本", "备注"),
            (
                (
                    item.role,
                    _format_number(item.direct_lag_seconds),
                    _format_number(item.correlation_lag_seconds),
                    _format_number(item.peak_correlation),
                    item.input_event_count,
                    item.frame_difference_count,
                    item.note,
                )
                for item in measurements
            ),
        ),
        "",
        "## 四个过渡段汇总",
        "",
    ]
    for label, values in (
        ("直接口径", [item.direct_lag_seconds for item in transition if item.direct_lag_seconds is not None]),
        ("相关口径", [item.correlation_lag_seconds for item in transition if item.correlation_lag_seconds is not None]),
    ):
        if values:
            lines.append(
                f"- {label}：n={len(values)}，中位 {statistics.median(values):.3f} 秒，"
                f"最小 {min(values):.3f}，最大 {max(values):.3f}，"
                f"总体标准差 {statistics.pstdev(values):.3f} 秒。"
            )
        else:
            lines.append(f"- {label}：不可用。")
    negative_peaks = [
        item
        for item in measurements
        if item.role in {"B-idle", "C-idle"}
        and item.correlation_lag_seconds is not None
    ]
    lines.extend(("", "## 负对照判定", ""))
    if negative_peaks:
        lines.append(
            "**负对照出现可计算峰，互相关结论全部作废并需重查：** "
            + "、".join(item.role for item in negative_peaks)
        )
    else:
        lines.append("B-idle 与 C-idle 均未产生可计算相关峰；未发现实现凭空匹配。")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return tuple(measurements)


def run_calibration(options: CalibrationOptions) -> Path:
    """Run the complete configured grid and write ignored evaluation artifacts."""
    if options.sample_stride <= 0:
        raise CaptureError("--sample-stride 必须大于 0")
    if not 0.0 <= options.strong_block_delta <= 1.0:
        raise CaptureError("--strong-block-delta 必须在 0 到 1 之间")
    if options.input_motion_threshold < 0.0:
        raise CaptureError("--input-motion-threshold 不能为负数")
    started = time.perf_counter()
    grid = load_grid(options.grid_path)
    combinations = grid.combinations()
    parsed_specs = calibration_segment_specs(options)
    sessions: list[PreparedCalibrationSession] = []
    for spec in parsed_specs:
        print(
            f"准备会话/区间：{spec.role} = {spec.session_dir} "
            f"[{0.0 if spec.start_seconds is None else spec.start_seconds:g}, "
            f"{'全段' if spec.end_seconds is None else f'{spec.end_seconds:g}'})"
        )
        session = prepare_session(
            spec.role,
            spec.session_dir,
            sample_stride=options.sample_stride,
            strong_block_delta=options.strong_block_delta,
            input_motion_threshold=options.input_motion_threshold,
            start_seconds=spec.start_seconds,
            end_seconds=spec.end_seconds,
        )
        sessions.append(session)
        print(
            f"  raw {session.original_raw_count} 张；预热+统计 {len(session.frames)} 张；"
            f"统计范围 {session.frame_count} 张；"
            f"input.csv {'可用' if session.input_available else '不可用'}"
        )
    print(
        f"开始网格：{len(combinations)} 组 × {len(sessions)} 会话 = "
        f"{len(combinations) * len(sessions)} 行"
    )
    results: list[CalibrationResult] = []
    for session in sessions:
        session_started = time.perf_counter()
        for combination_index, parameters in enumerate(combinations, start=1):
            results.append(
                evaluate_session(
                    session,
                    parameters,
                    combination_index=combination_index,
                )
            )
            if combination_index % 100 == 0:
                print(f"  {session.role}：{combination_index}/{len(combinations)}")
        print(
            f"完成 {session.role}：{time.perf_counter() - session_started:.3f} 秒"
        )

    first_result = evaluate_session(sessions[0], combinations[0], combination_index=1)
    second_result = evaluate_session(sessions[0], combinations[0], combination_index=1)
    first_bytes = _result_bytes(first_result)
    second_bytes = _result_bytes(second_result)
    deterministic = first_bytes == second_bytes
    deterministic_hash = hashlib.sha256(first_bytes).hexdigest()

    elapsed = time.perf_counter() - started
    options.output_dir.mkdir(parents=True, exist_ok=True)
    result_path = options.output_dir / "grid-results.csv"
    summary_path = options.output_dir / "summary.md"
    write_grid_results(result_path, results)
    write_summary(
        summary_path,
        sessions=sessions,
        results=results,
        grid=grid,
        options=options,
        elapsed_seconds=elapsed,
        deterministic=deterministic,
        deterministic_hash=deterministic_hash,
    )
    crosscorr_path = options.output_dir / "crosscorr.md"
    write_crosscorr(
        crosscorr_path,
        specs=parsed_specs,
        strong_block_delta=options.strong_block_delta,
    )
    print(f"确定性：{'通过' if deterministic else '失败'}；SHA-256 {deterministic_hash}")
    print(f"实际运行时长：{elapsed:.3f} 秒")
    print(f"结果：{result_path}")
    print(f"汇总：{summary_path}")
    print(f"互相关：{crosscorr_path}")
    if not deterministic:
        raise CaptureError("相同会话与参数的两次结果字节不一致")
    return options.output_dir
