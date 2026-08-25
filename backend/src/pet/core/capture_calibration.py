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
    DEFAULT_CAMERA_MOTION_RATIO,
    DEFAULT_INPUT_MOTION_THRESHOLD,
    DEFAULT_MAX_SILENCE_SECONDS,
    DEFAULT_MIN_SAVE_INTERVAL_SECONDS,
    DEFAULT_NOISE_MARGIN,
    DEFAULT_NOISE_MULTIPLIER,
    DEFAULT_NOISE_WINDOW,
    DEFAULT_PERSISTENCE_POLLS,
    DEFAULT_STRONG_BLOCK_DELTA,
    AdaptiveFrameSelector,
    CaptureError,
    DecisionReason,
    FrameChangeDetector,
    PreparedFrame,
    _raw_frame_timestamp,
)

DEFAULT_NOISE_WINDOWS = (10, 20, 40)
DEFAULT_NOISE_MULTIPLIERS = (1.5, 2.0, 2.5, 3.5)
DEFAULT_NOISE_MARGIN_PIXELS = (2.0, 4.0, 8.0)
DEFAULT_PERSISTENCE_VALUES = (1, 2, 3)
DEFAULT_CAMERA_MOTION_RATIOS = (0.20, 0.28, 0.35, 0.50)
CAMERA_TRUTH_MOUSE_PERCENTILE = 0.90
TOP_COMBINATION_COUNT = 20
TRADEOFF_POINT_LIMIT = 30


@dataclass(frozen=True, slots=True)
class CalibrationOptions:
    """Inputs for one deterministic calibration run."""

    session_specs: tuple[str, ...]
    output_dir: Path
    grid_path: Path | None = None
    sample_stride: int = 1
    strong_block_delta: float = DEFAULT_STRONG_BLOCK_DELTA
    input_motion_threshold: float = DEFAULT_INPUT_MOTION_THRESHOLD


@dataclass(frozen=True, slots=True)
class CalibrationParameters:
    """One point in the five-dimensional selector search space."""

    noise_window: int
    noise_multiplier: float
    noise_margin: float
    persistence_polls: int
    camera_motion_ratio: float

    @property
    def noise_margin_pixels(self) -> float:
        return self.noise_margin * 255.0

    @property
    def identifier(self) -> str:
        return (
            f"n{self.noise_window}-k{self.noise_multiplier:g}-"
            f"m{self.noise_margin_pixels:g}-p{self.persistence_polls}-"
            f"c{self.camera_motion_ratio:g}"
        )


@dataclass(frozen=True, slots=True)
class CalibrationGrid:
    """Validated values used to construct a full Cartesian product."""

    noise_windows: tuple[int, ...]
    noise_multipliers: tuple[float, ...]
    noise_margin_pixels: tuple[float, ...]
    persistence_values: tuple[int, ...]
    camera_motion_ratios: tuple[float, ...]

    def combinations(self) -> tuple[CalibrationParameters, ...]:
        return tuple(
            CalibrationParameters(
                noise_window=noise_window,
                noise_multiplier=noise_multiplier,
                noise_margin=noise_margin_pixels / 255.0,
                persistence_polls=persistence,
                camera_motion_ratio=camera_ratio,
            )
            for (
                noise_window,
                noise_multiplier,
                noise_margin_pixels,
                persistence,
                camera_ratio,
            ) in itertools.product(
                self.noise_windows,
                self.noise_multipliers,
                self.noise_margin_pixels,
                self.persistence_values,
                self.camera_motion_ratios,
            )
        )


@dataclass(frozen=True, slots=True)
class InputEvent:
    """One privacy-filtered row loaded from a session input.csv."""

    timestamp: datetime
    event_type: str
    dx: int
    dy: int


@dataclass(frozen=True, slots=True)
class PreparedCalibrationSession:
    """Read-only cached raw stream and independently derived truth proxies."""

    role: str
    session_dir: Path
    recorded_label: str
    paths: tuple[Path, ...]
    timestamps: tuple[datetime, ...]
    frames: tuple[PreparedFrame, ...]
    strong_adjacent_changes: tuple[bool, ...]
    input_available: bool
    input_activity: tuple[bool, ...] | None
    input_motion: tuple[float, ...] | None
    original_raw_count: int
    sample_stride: int
    strong_block_delta: float
    input_motion_threshold: float

    @property
    def duration_seconds(self) -> float:
        if len(self.timestamps) < 2:
            return 0.0
        return (self.timestamps[-1] - self.timestamps[0]).total_seconds()


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

    def reason_count(self, reason: DecisionReason) -> int:
        return dict(self.reason_counts)[reason]


@dataclass(frozen=True, slots=True)
class CameraMotionValidation:
    """Mouse-derived camera-motion proxy result for one ratio."""

    camera_motion_ratio: float
    mouse_percentile: float
    mouse_motion_threshold: float
    large_turn_polls: int
    non_turn_polls: int
    true_positive_polls: int
    false_positive_polls: int
    recall: float | None
    false_positive_rate: float | None


def default_grid() -> CalibrationGrid:
    return CalibrationGrid(
        noise_windows=DEFAULT_NOISE_WINDOWS,
        noise_multipliers=DEFAULT_NOISE_MULTIPLIERS,
        noise_margin_pixels=DEFAULT_NOISE_MARGIN_PIXELS,
        persistence_values=DEFAULT_PERSISTENCE_VALUES,
        camera_motion_ratios=DEFAULT_CAMERA_MOTION_RATIOS,
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
    return CalibrationGrid(
        noise_windows=_integer_values(grid, "noise_window", minimum=1),
        noise_multipliers=_float_values(grid, "noise_multiplier", minimum=0.0),
        noise_margin_pixels=_float_values(
            grid, "noise_margin_pixels", minimum=0.0, maximum=255.0
        ),
        persistence_values=_integer_values(
            grid, "persistence_polls", minimum=1
        ),
        camera_motion_ratios=_float_values(
            grid, "camera_motion_ratio", minimum=0.0, maximum=1.0
        ),
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


def prepare_session(
    role: str,
    session_dir: Path,
    *,
    sample_stride: int,
    strong_block_delta: float,
    input_motion_threshold: float,
) -> PreparedCalibrationSession:
    """Decode one immutable recording and precompute parameter-independent facts."""
    if sample_stride <= 0:
        raise ValueError("sample_stride must be positive")
    raw_dir = session_dir / "raw"
    all_paths = sorted(raw_dir.glob("raw-*.jpg"))
    if not all_paths:
        raise CaptureError(f"会话没有可校准的 raw JPEG：{raw_dir}")
    sampled_paths = tuple(all_paths[::sample_stride])
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
        events = _load_input_events(input_path)
        input_activity, input_motion = _align_input_events(
            events,
            tuple(timestamps),
            input_motion_threshold=input_motion_threshold,
        )
        input_available = True
    else:
        input_activity = None
        input_motion = None
        input_available = False

    recorded_label = _recorded_label(session_dir)
    return PreparedCalibrationSession(
        role=role,
        session_dir=session_dir,
        recorded_label=recorded_label,
        paths=sampled_paths,
        timestamps=tuple(timestamps),
        frames=tuple(frames),
        strong_adjacent_changes=tuple(strong_changes),
        input_available=input_available,
        input_activity=input_activity,
        input_motion=input_motion,
        original_raw_count=len(all_paths),
        sample_stride=sample_stride,
        strong_block_delta=strong_block_delta,
        input_motion_threshold=input_motion_threshold,
    )


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


def _load_input_events(path: Path) -> tuple[InputEvent, ...]:
    events: list[InputEvent] = []
    try:
        with path.open(encoding="utf-8-sig", newline="") as source:
            for row_number, row in enumerate(csv.DictReader(source), start=2):
                try:
                    timestamp = datetime.fromisoformat(row["时间"])
                    event_type = row["事件类型"]
                    dx = int(row["dx"] or 0)
                    dy = int(row["dy"] or 0)
                except (KeyError, TypeError, ValueError) as error:
                    raise CaptureError(
                        f"输入遥测格式无效：{path}:{row_number}"
                    ) from error
                events.append(InputEvent(timestamp, event_type, dx, dy))
    except OSError as error:
        raise CaptureError(f"无法读取输入遥测 {path}：{error}") from error
    events.sort(key=lambda event: event.timestamp)
    return tuple(events)


def _align_input_events(
    events: Sequence[InputEvent],
    timestamps: tuple[datetime, ...],
    *,
    input_motion_threshold: float,
) -> tuple[tuple[bool, ...], tuple[float, ...]]:
    """Aggregate input rows into the same half-open polling windows as frames."""
    activity: list[bool] = []
    motion: list[float] = []
    event_index = 0
    for timestamp in timestamps:
        key_event = False
        motion_total = 0.0
        while event_index < len(events) and events[event_index].timestamp <= timestamp:
            event = events[event_index]
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
        camera_motion_ratio=parameters.camera_motion_ratio,
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
    previous_timestamp: datetime | None = None
    for index, (frame, timestamp) in enumerate(
        zip(session.frames, session.timestamps, strict=True)
    ):
        decision = selector.observe_prepared(frame, timestamp.timestamp())
        reason_counts[decision.reason] += 1
        saved_count += int(decision.should_save)
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
            if previous_timestamp is not None:
                current_silence += (timestamp - previous_timestamp).total_seconds()
                longest_silence = max(longest_silence, current_silence)
        else:
            current_silence = 0.0
        previous_timestamp = timestamp
    frame_count = len(session.frames)
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
    )


def validate_camera_motion(
    session: PreparedCalibrationSession,
    camera_ratios: Sequence[float],
) -> tuple[CameraMotionValidation, ...]:
    """Compare selector camera labels with the P90 mouse-motion proxy."""
    if session.input_motion is None:
        return ()
    motion_values = list(session.input_motion)
    threshold = _percentile(sorted(motion_values), CAMERA_TRUTH_MOUSE_PERCENTILE)
    large_turn = tuple(value >= threshold and value > 0.0 for value in motion_values)
    validations: list[CameraMotionValidation] = []
    for ratio in camera_ratios:
        selector = AdaptiveFrameSelector(
            noise_window=DEFAULT_NOISE_WINDOW,
            noise_multiplier=DEFAULT_NOISE_MULTIPLIER,
            noise_margin=DEFAULT_NOISE_MARGIN,
            persistence_polls=DEFAULT_PERSISTENCE_POLLS,
            camera_motion_ratio=ratio,
            min_save_interval=DEFAULT_MIN_SAVE_INTERVAL_SECONDS,
            max_silence=DEFAULT_MAX_SILENCE_SECONDS,
        )
        camera_labels = tuple(
            selector.observe_prepared(frame, timestamp.timestamp()).camera_motion
            for frame, timestamp in zip(
                session.frames, session.timestamps, strict=True
            )
        )
        true_count = sum(large_turn)
        non_turn_count = len(large_turn) - true_count
        true_positive = sum(
            truth and predicted
            for truth, predicted in zip(large_turn, camera_labels, strict=True)
        )
        false_positive = sum(
            not truth and predicted
            for truth, predicted in zip(large_turn, camera_labels, strict=True)
        )
        validations.append(
            CameraMotionValidation(
                camera_motion_ratio=ratio,
                mouse_percentile=CAMERA_TRUTH_MOUSE_PERCENTILE,
                mouse_motion_threshold=threshold,
                large_turn_polls=true_count,
                non_turn_polls=non_turn_count,
                true_positive_polls=true_positive,
                false_positive_polls=false_positive,
                recall=None if true_count == 0 else true_positive / true_count,
                false_positive_rate=(
                    None
                    if non_turn_count == 0
                    else false_positive / non_turn_count
                ),
            )
        )
    return tuple(validations)


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
    "camera_motion_ratio",
    "会话角色",
    "会话目录",
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
    "camera_motion次数",
    "camera_motion占比",
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
                    _decimal(parameters.camera_motion_ratio),
                    result.session_role,
                    result.session_directory,
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
) -> list[tuple[CalibrationParameters, int, int, float, int]]:
    ranked: list[tuple[CalibrationParameters, int, int, float, int]] = []
    for parameters, rows in _group_by_parameters(results).items():
        local_total = sum(row.local_strong_misses for row in rows)
        input_rows = [row for row in rows if row.input_activity_misses is not None]
        input_total = sum(int(row.input_activity_misses) for row in input_rows)
        ranked.append(
            (
                parameters,
                local_total,
                input_total,
                statistics.mean(row.upload_rate for row in rows),
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


def _format_percent(value: float | None) -> str:
    return "不可用" if value is None else f"{value:.2%}"


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


def _tradeoff_rows(
    results: Sequence[CalibrationResult],
) -> tuple[list[tuple[CalibrationParameters, float, int, int, int]], int] | None:
    grouped = _group_by_parameters(results)
    points: list[tuple[CalibrationParameters, float, int, int, int]] = []
    for parameters, rows in grouped.items():
        idle = next((row for row in rows if row.session_role == "idle-3a"), None)
        text = next((row for row in rows if row.session_role == "text-heavy"), None)
        if idle is None or text is None:
            return None
        text_input = text.input_activity_misses or 0
        points.append(
            (
                parameters,
                idle.upload_rate,
                text.local_strong_misses,
                text_input,
                text.local_strong_misses + text_input,
            )
        )
    frontier = [
        point
        for point in points
        if not any(
            other[1] <= point[1]
            and other[4] <= point[4]
            and (other[1] < point[1] or other[4] < point[4])
            for other in points
        )
    ]
    frontier.sort(key=lambda point: (point[1], point[4], point[0].identifier))
    return frontier[:TRADEOFF_POINT_LIMIT], len(frontier)


def write_summary(
    path: Path,
    *,
    sessions: Sequence[PreparedCalibrationSession],
    results: Sequence[CalibrationResult],
    grid: CalibrationGrid,
    options: CalibrationOptions,
    camera_validation: Sequence[CameraMotionValidation],
    elapsed_seconds: float,
    deterministic: bool,
    deterministic_hash: str,
) -> None:
    ranked = _ranked_combinations(results)
    lines = [
        "# M5-T4 检测器参数校准数据",
        "",
        "本报告只列离线测量数据，不给出参数选型结论。评判顺序固定为：漏帧率优先，上传率次之。多传一帧的成本很低；漏掉的瞬间无法事后恢复。",
        "",
        "## 口径与运行信息",
        "",
        f"- 参数组合：{len(grid.combinations())} 组",
        f"- 会话：{len(sessions)} 个",
        f"- `grid-results.csv` 数据行：{len(results)}（{len(grid.combinations())} × {len(sessions)}）",
        f"- `sample_stride`：{options.sample_stride}（抽样率约 {1 / options.sample_stride:.2%}）",
        (
            f"- 局部剧变阈值：{options.strong_block_delta:.9f}"
            f"（{options.strong_block_delta * 255:.3f}/255；初始值待校准）"
        ),
        f"- 输入鼠标位移阈值：{options.input_motion_threshold:.3f} 像素/轮询窗口（初始值待校准）",
        "- 局部剧变漏检：相邻纳入重放的帧中任一 16×9 块平均差严格大于阈值，而本轮理由为 `no_change`。",
        "- 输入活动漏检：轮询窗口内存在按下/抬起，或鼠标相对位移累计严格大于阈值，而本轮理由为 `no_change`。缺少 input.csv 时为不可用。",
        "- 最长静默：连续 `no_change` 轮询覆盖的实际墙钟时长。",
        f"- 总运行时长：{elapsed_seconds:.3f} 秒",
        f"- 确定性复核：{'通过' if deterministic else '失败'}；两次结果字节 SHA-256 `{deterministic_hash}`",
        "- 会话角色取自 CLI 的 `角色=目录` 左侧；工具不根据游戏内容推断角色真实性。",
        "",
        "## 会话基本情况",
        "",
        _markdown_table(
            ("角色", "原录制标签", "目录", "原始帧数", "参与帧数", "时长秒", "input.csv"),
            (
                (
                    session.role,
                    session.recorded_label,
                    session.session_dir.name,
                    session.original_raw_count,
                    len(session.frames),
                    f"{session.duration_seconds:.3f}",
                    "可用" if session.input_available else "不可用",
                )
                for session in sessions
            ),
        ),
        "",
        "## 三级排序前若干组合",
        "",
        "排序键依次为：全部会话局部剧变漏检总数升序、可用会话输入活动漏检总数升序、会话平均上传率升序。",
        "",
        _markdown_table(
            ("名次", "参数组合", "局部剧变漏检总数", "输入活动漏检总数", "输入可用会话数", "平均上传率"),
            (
                (rank, item[0].identifier, item[1], item[2], item[4], f"{item[3]:.2%}")
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
        ("持久性 P", "persistence_polls", lambda value: str(value)),
        ("镜头移动占比", "camera_motion_ratio", lambda value: f"{value:.2f}"),
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
    lines.extend(("## idle-3a 上传率与 text-heavy 漏检权衡曲线", ""))
    tradeoff = _tradeoff_rows(results)
    if tradeoff is None:
        lines.append("不可用：输入会话角色中缺少 `idle-3a` 或 `text-heavy`。")
    else:
        points, total = tradeoff
        lines.extend(
            (
                (
                    f"下表为双目标非支配点，完整非支配点共 {total} 个；"
                    f"最多展示 {TRADEOFF_POINT_LIMIT} 个。text-heavy 总漏检 = "
                    "局部剧变漏检 + 输入活动漏检。"
                ),
                "",
                _markdown_table(
                    ("参数组合", "idle 上传率", "text 局部漏检", "text 输入漏检", "text 总漏检"),
                    (
                        (point[0].identifier, f"{point[1]:.2%}", point[2], point[3], point[4])
                        for point in points
                    ),
                ),
            )
        )
    lines.extend(("", "## explore-3a 镜头移动阈值真值校验", ""))
    if not camera_validation:
        lines.append("不可用：`explore-3a` 会话缺失或没有 input.csv。")
    else:
        first = camera_validation[0]
        lines.extend(
            (
                (
                    "大幅转视角代理：每轮询窗口累计鼠标相对位移的 "
                    f"P{int(first.mouse_percentile * 100)}，阈值为 "
                    f"{first.mouse_motion_threshold:.3f}。其他四个检测参数"
                    "固定为当前初值，只改变 camera_motion_ratio。"
                ),
                "",
                _markdown_table(
                    ("camera_motion_ratio", "大幅转向轮次", "非转向轮次", "召回轮次", "误判轮次", "召回率", "误判率"),
                    (
                        (
                            f"{row.camera_motion_ratio:.2f}",
                            row.large_turn_polls,
                            row.non_turn_polls,
                            row.true_positive_polls,
                            row.false_positive_polls,
                            _format_percent(row.recall),
                            _format_percent(row.false_positive_rate),
                        )
                        for row in camera_validation
                    ),
                ),
            )
        )
    missing = [session.role for session in sessions if not session.input_available]
    lines.extend(("", "## 输入指标不可用会话", ""))
    lines.append("无。" if not missing else "、".join(missing) + "（对应 CSV 单元格留空，不填零）。")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def run_calibration(options: CalibrationOptions) -> Path:
    """Run the complete configured grid and write ignored evaluation artifacts."""
    if not options.session_specs:
        raise CaptureError("--calibrate 至少需要一个会话目录")
    if options.sample_stride <= 0:
        raise CaptureError("--sample-stride 必须大于 0")
    if not 0.0 <= options.strong_block_delta <= 1.0:
        raise CaptureError("--strong-block-delta 必须在 0 到 1 之间")
    if options.input_motion_threshold < 0.0:
        raise CaptureError("--input-motion-threshold 不能为负数")
    started = time.perf_counter()
    grid = load_grid(options.grid_path)
    combinations = grid.combinations()
    parsed_specs = tuple(parse_session_spec(spec) for spec in options.session_specs)
    if len({role for role, _ in parsed_specs}) != len(parsed_specs):
        raise CaptureError("校准会话角色必须唯一")
    sessions: list[PreparedCalibrationSession] = []
    for role, session_dir in parsed_specs:
        print(f"准备会话：{role} = {session_dir}")
        session = prepare_session(
            role,
            session_dir,
            sample_stride=options.sample_stride,
            strong_block_delta=options.strong_block_delta,
            input_motion_threshold=options.input_motion_threshold,
        )
        sessions.append(session)
        print(
            f"  raw {session.original_raw_count} 张；参与 {len(session.frames)} 张；"
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

    explore = next((session for session in sessions if session.role == "explore-3a"), None)
    camera_validation = (
        ()
        if explore is None
        else validate_camera_motion(explore, grid.camera_motion_ratios)
    )
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
        camera_validation=camera_validation,
        elapsed_seconds=elapsed,
        deterministic=deterministic,
        deterministic_hash=deterministic_hash,
    )
    print(f"确定性：{'通过' if deterministic else '失败'}；SHA-256 {deterministic_hash}")
    print(f"实际运行时长：{elapsed:.3f} 秒")
    print(f"结果：{result_path}")
    print(f"汇总：{summary_path}")
    if not deterministic:
        raise CaptureError("相同会话与参数的两次结果字节不一致")
    return options.output_dir
