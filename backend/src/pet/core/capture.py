"""Single-window capture and a local frame-change probe.

The production-facing classes in this module never select a monitor or the
desktop.  A Windows HWND is resolved first, then passed to Windows Graphics
Capture through the optional ``zbl`` binding.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import importlib
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from types import ModuleType
from typing import Literal, Protocol, Self

import numpy as np
import numpy.typing as npt
from PIL import Image

DEFAULT_INTERVAL_SECONDS = 2.0
DEFAULT_THRESHOLD = 0.02
DEFAULT_CHANGE_WIDTH = 96
# 此数待实测确定。
DEFAULT_AREA_WIDTH = 320
# 此数待实测确定。
DEFAULT_PIXEL_DELTA_THRESHOLD = 24
# 此数待实测确定。
DEFAULT_BLOCK_GRID = (16, 9)
# 此数待实测确定。
DEFAULT_BLOCK_DELTA_THRESHOLD = 12
DEFAULT_MIN_SAVE_INTERVAL_SECONDS = 1.0
DEFAULT_MAX_SILENCE_SECONDS = 60.0
DEFAULT_MAX_FILES = 500
DEFAULT_MAX_BYTES = 200 * 1024 * 1024
FOREGROUND_SELECTION_DELAY_SECONDS = 3
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# zbl 0.7.1 buffers 32 source frames and drops newly arrived frames when that
# queue is full. Draining at most twice that bound handles one concurrent
# refill without allowing a hot producer to keep a poll inside this loop.
MAX_DRAINED_SOURCE_FRAMES_PER_POLL = 64


class CaptureError(RuntimeError):
    """A capture failure phrased for a person running the probe."""


@dataclass(frozen=True, slots=True)
class WindowTarget:
    """One exact Windows window selected for capture."""

    hwnd: int
    title: str
    process_name: str


@dataclass(frozen=True, slots=True)
class FrameMetadata:
    """Metadata recorded at the time one target-window frame is copied."""

    window_title: str
    process_name: str
    captured_at: datetime
    width: int
    height: int
    source_frames_drained: int = 1


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    """An owned RGBA bitmap and its target-window metadata."""

    bitmap: Image.Image
    metadata: FrameMetadata


class _CaptureSession(Protocol):
    """The small part of zbl used by the game-independent wrapper."""

    @property
    def handle(self) -> int: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, *args: object) -> None: ...

    def try_grab(self) -> npt.NDArray[np.uint8] | None: ...


def _is_windows() -> bool:
    return sys.platform == "win32"


def _windows_error(action: str) -> CaptureError:
    error_code = ctypes.get_last_error()
    detail = ctypes.WinError(error_code) if error_code else "未知 Windows 错误"
    return CaptureError(f"{action}失败：{detail}")


def _configured_user32() -> object:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.IsWindow.argtypes = [ctypes.c_void_p]
    user32.IsWindow.restype = ctypes.c_bool
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = ctypes.c_bool
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    return user32


def _configured_kernel32() -> object:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    return kernel32


def _window_title(hwnd: int, user32: object) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length < 0:
        raise _windows_error("读取窗口标题")
    buffer = ctypes.create_unicode_buffer(length + 1)
    copied = user32.GetWindowTextW(hwnd, buffer, len(buffer))
    if copied == 0 and length > 0:
        raise _windows_error("读取窗口标题")
    return buffer.value or "（无标题窗口）"


def _process_name(hwnd: int, user32: object) -> str:
    process_id = ctypes.c_ulong()
    if user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id)) == 0:
        raise _windows_error("读取窗口所属进程")

    kernel32 = _configured_kernel32()
    process = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        process_id.value,
    )
    if not process:
        raise CaptureError(
            "已找到目标窗口，但无法读取进程名；请用与游戏相同权限级别运行探针"
        )
    try:
        buffer = ctypes.create_unicode_buffer(32_768)
        size = ctypes.c_ulong(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(
            process,
            0,
            buffer,
            ctypes.byref(size),
        ):
            raise _windows_error("读取窗口所属进程名")
        return PureWindowsPath(buffer.value).name
    finally:
        kernel32.CloseHandle(process)


def _target_from_hwnd(hwnd: int, user32: object) -> WindowTarget:
    if not hwnd or not user32.IsWindow(hwnd):
        raise CaptureError("目标窗口已经关闭或不是有效窗口，请重新启动探针")
    return WindowTarget(
        hwnd=hwnd,
        title=_window_title(hwnd, user32),
        process_name=_process_name(hwnd, user32),
    )


def _resolve_window(title_filter: str | None) -> WindowTarget:
    user32 = _configured_user32()
    if title_filter is None:
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            raise CaptureError("没有可捕获的前台窗口；请先切到游戏窗口再运行探针")
        return _target_from_hwnd(hwnd, user32)

    needle = title_filter.casefold().strip()
    if not needle:
        raise CaptureError("--title 不能为空；请给出窗口标题中的一段文字")
    matches: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def collect(hwnd_pointer: int | None, _parameter: int | None) -> bool:
        hwnd = int(hwnd_pointer or 0)
        if hwnd and user32.IsWindowVisible(hwnd):
            title = _window_title(hwnd, user32)
            if needle in title.casefold():
                matches.append(hwnd)
        return True

    user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
    user32.EnumWindows.restype = ctypes.c_bool
    if not user32.EnumWindows(collect, None):
        raise _windows_error("枚举窗口")
    if not matches:
        raise CaptureError(
            f"没有找到标题包含“{title_filter}”的可见窗口；请先启动游戏并检查标题文字"
        )
    return _target_from_hwnd(matches[0], user32)


def _load_zbl() -> ModuleType:
    try:
        return importlib.import_module("zbl")
    except (ImportError, OSError) as error:
        raise CaptureError(
            "缺少 Windows Graphics Capture 组件；请在 backend 下按 requirements.txt "
            "安装依赖后重试"
        ) from error


class WindowsGraphicsCaptureBackend:
    """Own one persistent WGC session for one exact target window."""

    def __init__(self, title_filter: str | None = None) -> None:
        if not _is_windows():
            raise CaptureError(
                "窗口截屏只支持 Windows 10/11；当前系统不能初始化 Windows Graphics Capture"
            )
        self._target = _resolve_window(title_filter)
        self._session: _CaptureSession | None = None
        zbl = _load_zbl()
        capture_type = getattr(zbl, "Capture", None)
        if capture_type is None:
            raise CaptureError("已安装的 zbl 不包含 Capture；请重新安装 requirements.txt")
        try:
            session = capture_type(
                window_handle=self._target.hwnd,
                is_cursor_capture_enabled=False,
                is_border_required=True,
                use_staging_texture=True,
            )
            self._session = session.__enter__()
        except Exception as error:
            raise CaptureError(
                "Windows Graphics Capture 初始化失败；请确认系统为 Windows 10 1903+，"
                "在本地交互桌面会话中运行，并让探针与游戏权限级别一致。"
                f"原始错误：{error}"
            ) from error

    @property
    def target(self) -> WindowTarget:
        return self._target

    def capture_frame(self) -> CapturedFrame | None:
        """Return the newest queued WGC frame without ever waiting for one.

        zbl's blocking ``grab`` busy-waits while its queue is empty. Its
        bounded FIFO also drops newly arriving frames when full, so reading one
        item per slow probe poll can return stale history. Drain the bounded
        queue non-blockingly and retain only its newest available frame.
        """
        if self._session is None:
            raise CaptureError("截屏会话已经关闭；请重新启动探针")
        try:
            latest: npt.NDArray[np.uint8] | None = None
            source_frames_drained = 0
            while source_frames_drained < MAX_DRAINED_SOURCE_FRAMES_PER_POLL:
                candidate = self._session.try_grab()
                if candidate is None:
                    break
                latest = candidate
                source_frames_drained += 1
        except StopIteration as error:
            raise CaptureError("目标窗口已关闭，截屏会话随之结束") from error
        except Exception as error:
            raise CaptureError(f"抓取目标窗口失败：{error}") from error
        if latest is None:
            return None

        # The array points at zbl's reusable native staging texture. Own the
        # newest contents before another source frame can overwrite it.
        raw = np.asarray(latest, dtype=np.uint8).copy()
        if raw.ndim != 3 or raw.shape[2] != 4 or raw.size == 0:
            raise CaptureError(
                f"WGC 返回了无法识别的位图形状 {raw.shape}；请记录游戏和显示模式"
            )

        # WGC's D3D11 frame is BGRA8.  Copy and reorder before the next native grab.
        rgba = np.ascontiguousarray(raw[..., [2, 1, 0, 3]])
        bitmap = Image.fromarray(rgba, mode="RGBA")
        user32 = _configured_user32()
        current_target = _target_from_hwnd(self._target.hwnd, user32)
        self._target = current_target
        return CapturedFrame(
            bitmap=bitmap,
            metadata=FrameMetadata(
                window_title=current_target.title,
                process_name=current_target.process_name,
                captured_at=datetime.now(timezone.utc),
                width=bitmap.width,
                height=bitmap.height,
                source_frames_drained=source_frames_drained,
            ),
        )

    def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            session.__exit__(None, None, None)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


FrameLike = Image.Image | npt.NDArray[np.generic]
MetricName = Literal["mean_amplitude", "changed_area", "block_change"]
BaselineName = Literal["vs_previous", "vs_baseline"]
StrategyName = Literal[
    "mean_amplitude_vs_previous",
    "mean_amplitude_vs_baseline",
    "changed_area_vs_previous",
    "changed_area_vs_baseline",
    "block_change_vs_previous",
    "block_change_vs_baseline",
]
STRATEGY_NAMES: tuple[StrategyName, ...] = (
    "mean_amplitude_vs_previous",
    "mean_amplitude_vs_baseline",
    "changed_area_vs_previous",
    "changed_area_vs_baseline",
    "block_change_vs_previous",
    "block_change_vs_baseline",
)


@dataclass(frozen=True, slots=True)
class FrameMetrics:
    """Three normalized, stateless measures for one frame pair."""

    mean_amplitude: float
    changed_area: float
    block_change: float

    def value(self, metric_name: MetricName) -> float:
        return float(getattr(self, metric_name))


@dataclass(frozen=True, slots=True)
class FrameComparisons:
    """All metrics against the previous and last-saved baselines."""

    vs_previous: FrameMetrics
    vs_baseline: FrameMetrics

    def strategy_value(self, strategy: StrategyName) -> float:
        metric_name, baseline_name = strategy.rsplit("_vs_", maxsplit=1)
        baseline: BaselineName = (
            "vs_previous" if baseline_name == "previous" else "vs_baseline"
        )
        metrics = getattr(self, baseline)
        return metrics.value(metric_name)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class PreparedFrame:
    """Cached grayscale reductions used by the three pure metrics."""

    mean_gray: npt.NDArray[np.float32]
    area_gray: npt.NDArray[np.float32]


@dataclass(frozen=True, slots=True)
class FrameChangeDetector:
    """Stateless, deterministic comparison of two injectable bitmaps."""

    threshold: float = DEFAULT_THRESHOLD
    target_width: int = DEFAULT_CHANGE_WIDTH
    area_width: int = DEFAULT_AREA_WIDTH
    pixel_delta_threshold: int = DEFAULT_PIXEL_DELTA_THRESHOLD
    block_grid: tuple[int, int] = DEFAULT_BLOCK_GRID
    block_delta_threshold: float = DEFAULT_BLOCK_DELTA_THRESHOLD

    def __post_init__(self) -> None:
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if self.target_width <= 0:
            raise ValueError("target_width must be positive")
        if self.area_width <= 0:
            raise ValueError("area_width must be positive")
        if not 0 <= self.pixel_delta_threshold <= 255:
            raise ValueError("pixel_delta_threshold must be between 0 and 255")
        if self.block_grid[0] <= 0 or self.block_grid[1] <= 0:
            raise ValueError("block_grid dimensions must be positive")
        if not 0.0 <= self.block_delta_threshold <= 255.0:
            raise ValueError("block_delta_threshold must be between 0 and 255")

    def difference(self, previous: FrameLike, current: FrameLike) -> float:
        """Return normalized mean grayscale pixel difference in the range 0..1."""
        previous_prepared, current_prepared = self.prepare_pair(previous, current)
        return self.compare_prepared(previous_prepared, current_prepared).mean_amplitude

    def has_changed(self, previous: FrameLike, current: FrameLike) -> bool:
        """Return true only when the pure difference score exceeds the threshold."""
        return self.difference(previous, current) > self.threshold

    def prepare(self, frame: FrameLike) -> PreparedFrame:
        """Reduce one injectable bitmap without retaining detector state."""
        gray = _to_grayscale(frame)
        return PreparedFrame(
            mean_gray=_resize_gray(gray, self.target_width),
            area_gray=_resize_gray(gray, self.area_width),
        )

    def prepare_pair(
        self, reference: FrameLike, current: FrameLike
    ) -> tuple[PreparedFrame, PreparedFrame]:
        """Prepare unlike-sized inputs to one shared aspect ratio."""
        reference_gray = _to_grayscale(reference)
        current_gray = _to_grayscale(current)
        aspect_ratio = min(
            reference_gray.height / reference_gray.width,
            current_gray.height / current_gray.width,
        )
        return (
            PreparedFrame(
                mean_gray=_resize_gray(reference_gray, self.target_width, aspect_ratio),
                area_gray=_resize_gray(reference_gray, self.area_width, aspect_ratio),
            ),
            PreparedFrame(
                mean_gray=_resize_gray(current_gray, self.target_width, aspect_ratio),
                area_gray=_resize_gray(current_gray, self.area_width, aspect_ratio),
            ),
        )

    def compare(self, reference: FrameLike, current: FrameLike) -> FrameMetrics:
        """Calculate all three metrics for an injectable pair."""
        reference_prepared, current_prepared = self.prepare_pair(reference, current)
        return self.compare_prepared(reference_prepared, current_prepared)

    def compare_prepared(
        self, reference: PreparedFrame, current: PreparedFrame
    ) -> FrameMetrics:
        """Calculate all metrics from cached grayscale reductions."""
        reference_mean, current_mean = _align_gray_arrays(
            reference.mean_gray, current.mean_gray
        )
        reference_area, current_area = _align_gray_arrays(
            reference.area_gray, current.area_gray
        )
        mean_difference = np.abs(reference_mean - current_mean)
        area_difference = np.abs(reference_area - current_area)
        changed_area = float(
            np.count_nonzero(area_difference > self.pixel_delta_threshold)
            / area_difference.size
        )
        columns, rows = self.block_grid
        changed_blocks = sum(
            float(block.mean()) > self.block_delta_threshold
            for row in np.array_split(area_difference, rows, axis=0)
            for block in np.array_split(row, columns, axis=1)
        )
        return FrameMetrics(
            mean_amplitude=float(mean_difference.mean() / 255.0),
            changed_area=changed_area,
            block_change=changed_blocks / (columns * rows),
        )


def _resize_gray(
    gray: Image.Image, target_width: int, aspect_ratio: float | None = None
) -> npt.NDArray[np.float32]:
    ratio = aspect_ratio if aspect_ratio is not None else gray.height / gray.width
    target_height = max(1, round(target_width * ratio))
    return np.asarray(
        gray.resize((target_width, target_height), Image.Resampling.BILINEAR),
        dtype=np.float32,
    )


def _align_gray_arrays(
    reference: npt.NDArray[np.float32], current: npt.NDArray[np.float32]
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.float32]]:
    if reference.shape == current.shape:
        return reference, current
    size = (min(reference.shape[1], current.shape[1]), min(reference.shape[0], current.shape[0]))

    def resized(array: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
        if array.shape == (size[1], size[0]):
            return array
        image = Image.fromarray(array.astype(np.uint8, copy=False), mode="L")
        return np.asarray(image.resize(size, Image.Resampling.BILINEAR), dtype=np.float32)

    return resized(reference), resized(current)


def _to_grayscale(frame: FrameLike) -> Image.Image:
    if isinstance(frame, Image.Image):
        if frame.width <= 0 or frame.height <= 0:
            raise ValueError("frame must not be empty")
        return frame.convert("L")

    array = np.asarray(frame)
    if array.size == 0 or array.ndim not in (2, 3):
        raise ValueError("frame must be a non-empty 2D or 3D image")
    if array.ndim == 3 and array.shape[2] not in (3, 4):
        raise ValueError("color frame must have 3 or 4 channels")
    uint8 = np.clip(array, 0, 255).astype(np.uint8, copy=False)
    mode = "L" if uint8.ndim == 2 else ("RGB" if uint8.shape[2] == 3 else "RGBA")
    return Image.fromarray(uint8, mode=mode).convert("L")


@dataclass(frozen=True, slots=True)
class ArchivedFrame:
    """One PNG currently retained inside the configured save directory."""

    path: Path
    size_bytes: int
    modified_ns: int


class CaptureArchive:
    """Save PNGs and enforce count/byte limits inside one explicit directory."""

    def __init__(
        self,
        save_dir: Path,
        *,
        max_files: int = DEFAULT_MAX_FILES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if max_files <= 0 or max_bytes <= 0:
            raise ValueError("archive limits must be positive")
        self.save_dir = save_dir
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self._entries = self._existing_pngs()
        self._enforce_limits()

    @property
    def retained_count(self) -> int:
        return len(self._entries)

    @property
    def retained_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self._entries)

    def save(self, frame: CapturedFrame, sequence: int) -> Path | None:
        timestamp = frame.metadata.captured_at.astimezone(timezone.utc).strftime(
            "%Y%m%dT%H%M%S.%fZ"
        )
        path = self.save_dir / f"frame-{sequence:06d}-{timestamp}.png"
        frame.bitmap.save(path, format="PNG")
        stat = path.stat()
        self._entries.append(
            ArchivedFrame(path=path, size_bytes=stat.st_size, modified_ns=stat.st_mtime_ns)
        )
        self._entries.sort(key=lambda entry: (entry.modified_ns, entry.path.name))
        self._enforce_limits()
        return path if path.exists() else None

    def _existing_pngs(self) -> list[ArchivedFrame]:
        entries: list[ArchivedFrame] = []
        for path in self.save_dir.glob("*.png"):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            entries.append(
                ArchivedFrame(
                    path=path,
                    size_bytes=stat.st_size,
                    modified_ns=stat.st_mtime_ns,
                )
            )
        entries.sort(key=lambda entry: (entry.modified_ns, entry.path.name))
        return entries

    def _enforce_limits(self) -> None:
        while (
            len(self._entries) > self.max_files
            or self.retained_bytes > self.max_bytes
        ):
            oldest = self._entries.pop(0)
            oldest.path.unlink(missing_ok=True)


@dataclass(slots=True)
class ProbeStatistics:
    """In-memory counters printed when the foreground probe exits."""

    poll_count: int = 0
    captured_count: int = 0
    unavailable_count: int = 0
    source_frames_drained: int = 0
    max_source_frames_drained: int = 0
    saved_count: int = 0
    forced_saved_count: int = 0
    capture_durations: list[float] = field(default_factory=list)
    metric_durations_ms: list[float] = field(default_factory=list)
    mean_amplitude_vs_previous: list[float] = field(default_factory=list)
    changed_area_vs_previous: list[float] = field(default_factory=list)
    block_change_vs_previous: list[float] = field(default_factory=list)
    mean_amplitude_vs_baseline: list[float] = field(default_factory=list)
    changed_area_vs_baseline: list[float] = field(default_factory=list)
    block_change_vs_baseline: list[float] = field(default_factory=list)

    def record(self, comparisons: FrameComparisons, duration_ms: float) -> None:
        self.metric_durations_ms.append(duration_ms)
        for strategy in STRATEGY_NAMES:
            getattr(self, strategy).append(comparisons.strategy_value(strategy))

    def metric_series(self) -> tuple[tuple[StrategyName, list[float]], ...]:
        return tuple((strategy, getattr(self, strategy)) for strategy in STRATEGY_NAMES)


@dataclass(frozen=True, slots=True)
class FrameObservation:
    """One current frame plus its six stateless comparison values."""

    prepared: PreparedFrame
    comparisons: FrameComparisons
    is_first: bool


class FrameComparisonTracker:
    """Own the two probe baselines while keeping metric calculation stateless."""

    def __init__(self, detector: FrameChangeDetector) -> None:
        self._detector = detector
        self._previous: PreparedFrame | None = None
        self._baseline: PreparedFrame | None = None

    def observe(self, frame: FrameLike) -> FrameObservation:
        prepared = self._detector.prepare(frame)
        is_first = self._previous is None
        previous = self._previous or prepared
        baseline = self._baseline or prepared
        comparisons = FrameComparisons(
            vs_previous=self._detector.compare_prepared(previous, prepared),
            vs_baseline=self._detector.compare_prepared(baseline, prepared),
        )
        self._previous = prepared
        if self._baseline is None:
            # The probe always archives its first frame, so it is the initial baseline.
            self._baseline = prepared
        return FrameObservation(prepared, comparisons, is_first)

    def mark_saved(self, prepared: PreparedFrame) -> None:
        self._baseline = prepared


@dataclass(frozen=True, slots=True)
class SaveDecision:
    """Whether this poll should be archived and whether silence forced it."""

    should_save: bool
    forced: bool = False


class SavePolicy:
    """Apply a selectable metric plus algorithm-independent timing safeguards."""

    def __init__(
        self,
        strategy: StrategyName,
        threshold: float,
        min_save_interval: float,
        max_silence: float,
    ) -> None:
        if strategy not in STRATEGY_NAMES:
            raise ValueError(f"unknown strategy: {strategy}")
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if min_save_interval < 0:
            raise ValueError("min_save_interval must not be negative")
        if max_silence <= 0:
            raise ValueError("max_silence must be positive")
        self.strategy = strategy
        self.threshold = threshold
        self.min_save_interval = min_save_interval
        self.max_silence = max_silence
        self._last_saved_at: float | None = None

    def decide(
        self,
        comparisons: FrameComparisons,
        now: float,
        *,
        is_first: bool = False,
    ) -> SaveDecision:
        if is_first or self._last_saved_at is None:
            return SaveDecision(True)
        since_saved = now - self._last_saved_at
        if since_saved >= self.max_silence:
            return SaveDecision(True, forced=True)
        changed = comparisons.strategy_value(self.strategy) > self.threshold
        return SaveDecision(changed and since_saved >= self.min_save_interval)

    def mark_saved(self, now: float) -> None:
        self._last_saved_at = now


CSV_HEADER = (
    "序号",
    "时间",
    "窗口标题",
    "平均振幅_对比上一帧",
    "变化面积_对比上一帧",
    "块变化_对比上一帧",
    "平均振幅_对比基线",
    "变化面积_对比基线",
    "块变化_对比基线",
    "是否落盘",
    "是否强制落盘",
    "本次落盘文件名",
    "本次指标计算耗时毫秒",
    "是否取得画面",
    "本次取出源帧数",
    "本次抓帧耗时毫秒",
    "截屏状态",
)


class MetricsCsvWriter:
    """Append and immediately flush one Chinese CSV row per poll."""

    def __init__(self, save_dir: Path) -> None:
        save_dir.mkdir(parents=True, exist_ok=True)
        self.path = save_dir / "metrics.csv"
        self._file = self.path.open("w", encoding="utf-8-sig", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(CSV_HEADER)
        self._file.flush()

    def write(
        self,
        sequence: int,
        captured_at: datetime,
        window_title: str,
        comparisons: FrameComparisons,
        saved: bool,
        forced: bool,
        saved_filename: str,
        duration_ms: float,
        *,
        source_frames_drained: int = 1,
        capture_duration_ms: float = 0.0,
    ) -> None:
        previous = comparisons.vs_previous
        baseline = comparisons.vs_baseline
        self._writer.writerow(
            (
                sequence,
                captured_at.isoformat(),
                window_title,
                f"{previous.mean_amplitude:.9f}",
                f"{previous.changed_area:.9f}",
                f"{previous.block_change:.9f}",
                f"{baseline.mean_amplitude:.9f}",
                f"{baseline.changed_area:.9f}",
                f"{baseline.block_change:.9f}",
                "是" if saved else "否",
                "是" if forced else "否",
                saved_filename,
                f"{duration_ms:.3f}",
                "是",
                source_frames_drained,
                f"{capture_duration_ms:.3f}",
                "正常",
            )
        )
        self._file.flush()

    def write_unavailable(
        self,
        sequence: int,
        attempted_at: datetime,
        window_title: str,
        capture_duration_ms: float,
    ) -> None:
        """Record and flush a poll where WGC had no frame ready."""
        self._writer.writerow(
            (
                sequence,
                attempted_at.isoformat(),
                window_title,
                "",
                "",
                "",
                "",
                "",
                "",
                "否",
                "否",
                "",
                "",
                "否",
                0,
                f"{capture_duration_ms:.3f}",
                "WGC 暂无新帧",
            )
        )
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class ProbeOptions:
    """Validated command-line options for the foreground watch loop."""

    interval: float
    threshold: float
    title: str | None
    save_dir: Path
    strategy: StrategyName = "mean_amplitude_vs_previous"
    min_save_interval: float = DEFAULT_MIN_SAVE_INTERVAL_SECONDS
    max_silence: float = DEFAULT_MAX_SILENCE_SECONDS
    label: str | None = None


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须大于或等于 0")
    return parsed


def _threshold(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("必须在 0 到 1 之间")
    return parsed


def _default_save_dir() -> Path:
    backend_root = Path(__file__).resolve().parents[3]
    started_at = datetime.now().strftime("%Y%m%d-%H%M%S")
    return backend_root / "recordings" / "capture" / started_at


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只抓一个游戏窗口的本地画面变化探针")
    parser.add_argument("--watch", action="store_true", help="持续抓帧，Ctrl+C 停止")
    parser.add_argument(
        "--interval",
        type=_positive_float,
        default=DEFAULT_INTERVAL_SECONDS,
        help="相邻采样间隔秒数（默认 2.0，待实测）",
    )
    parser.add_argument(
        "--threshold",
        type=_threshold,
        default=DEFAULT_THRESHOLD,
        help="变化阈值 0..1（默认 0.02，待实测）",
    )
    parser.add_argument(
        "--strategy",
        choices=STRATEGY_NAMES,
        default="mean_amplitude_vs_previous",
        help="选择用于落盘判定的指标和基线",
    )
    parser.add_argument(
        "--min-save-interval",
        type=_nonnegative_float,
        default=DEFAULT_MIN_SAVE_INTERVAL_SECONDS,
        help="两次落盘的最短间隔秒数（默认 1.0）",
    )
    parser.add_argument(
        "--max-silence",
        type=_positive_float,
        default=DEFAULT_MAX_SILENCE_SECONDS,
        help="最长无落盘时间，超时强制保存（默认 60.0 秒）",
    )
    parser.add_argument("--label", help="本次采集会话的可选标签")
    parser.add_argument("--title", help="目标窗口标题中的一段文字")
    parser.add_argument(
        "--save-dir",
        type=Path,
        help="PNG 保存目录（默认 backend/recordings/capture/<启动时间>/）",
    )
    return parser


def _configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _print_banner(options: ProbeOptions) -> None:
    print("=" * 72)
    print("【正在截屏】只抓一个目标窗口；不会抓全桌面或拼接显示器")
    print(f"保存目录：{options.save_dir}")
    print(
        f"策略：{options.strategy}；阈值：{options.threshold:.5f}；"
        f"最短落盘间隔：{options.min_save_interval:.1f}s；"
        f"最长静默：{options.max_silence:.1f}s"
    )
    print("首帧、策略判定变化帧及最长静默强制帧落盘；Ctrl+C 停止")
    print("=" * 72)


def _distribution(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "中位数": statistics.median(ordered),
        "P90": _percentile(ordered, 0.90),
        "P99": _percentile(ordered, 0.99),
        "最大值": ordered[-1],
    }


def _build_summary(
    statistics_: ProbeStatistics, archive: CaptureArchive
) -> dict[str, object]:
    metrics = {
        strategy: _distribution(values)
        for strategy, values in statistics_.metric_series()
    }
    metric_timing = _distribution(statistics_.metric_durations_ms)
    return {
        "总轮询数": statistics_.poll_count,
        "成功抓帧数": statistics_.captured_count,
        "无新帧轮询数": statistics_.unavailable_count,
        "累计取出源帧数": statistics_.source_frames_drained,
        "单次最多取出源帧数": statistics_.max_source_frames_drained,
        "落盘次数": statistics_.saved_count,
        "强制落盘次数": statistics_.forced_saved_count,
        "当前保留张数": archive.retained_count,
        "当前保留字节数": archive.retained_bytes,
        "六指标分布": metrics,
        "指标计算耗时毫秒": None
        if metric_timing is None
        else {
            "中位数": metric_timing["中位数"],
            "最大值": metric_timing["最大值"],
        },
        "抓帧耗时毫秒": None
        if not statistics_.capture_durations
        else {
            "中位数": statistics.median(statistics_.capture_durations) * 1000,
            "最大值": max(statistics_.capture_durations) * 1000,
        },
    }


def _print_summary(summary: dict[str, object]) -> None:
    print("\n截屏探针汇总")
    print(
        f"轮询数：{summary['总轮询数']}（成功抓帧 {summary['成功抓帧数']}；"
        f"无新帧 {summary['无新帧轮询数']}）"
    )
    print(
        f"WGC 源帧：累计取出 {summary['累计取出源帧数']}；"
        f"单次最多 {summary['单次最多取出源帧数']}"
    )
    print(
        f"落盘数：{summary['落盘次数']}（强制 {summary['强制落盘次数']}；"
        f"当前保留 {summary['当前保留张数']} 张，"
        f"{int(summary['当前保留字节数']) / (1024 * 1024):.2f} MB）"
    )
    metric_summaries = summary["六指标分布"]
    assert isinstance(metric_summaries, dict)
    for strategy in STRATEGY_NAMES:
        distribution = metric_summaries[strategy]
        if isinstance(distribution, dict):
            print(
                f"{strategy}：中位 {distribution['中位数']:.5f} / "
                f"P90 {distribution['P90']:.5f} / P99 {distribution['P99']:.5f} / "
                f"最大 {distribution['最大值']:.5f}"
            )
    metric_timing = summary["指标计算耗时毫秒"]
    if isinstance(metric_timing, dict):
        print(
            "六指标计算耗时："
            f"中位 {metric_timing['中位数']:.1f} ms / "
            f"最大 {metric_timing['最大值']:.1f} ms"
        )
    capture_timing = summary["抓帧耗时毫秒"]
    if isinstance(capture_timing, dict):
        print(
            "单次抓帧耗时："
            f"中位 {capture_timing['中位数']:.1f} ms / "
            f"最大 {capture_timing['最大值']:.1f} ms"
        )


def _write_session(
    options: ProbeOptions,
    started_at: datetime,
    ended_at: datetime | None,
    total_polls: int,
    summary: dict[str, object] | None,
) -> None:
    payload = {
        "标签": options.label,
        "启动参数": {
            "interval": options.interval,
            "threshold": options.threshold,
            "title": options.title,
            "save_dir": str(options.save_dir),
            "strategy": options.strategy,
            "min_save_interval": options.min_save_interval,
            "max_silence": options.max_silence,
        },
        "开始时间": started_at.isoformat(),
        "结束时间": None if ended_at is None else ended_at.isoformat(),
        "总轮询数": total_polls,
        "汇总": summary,
    }
    path = options.save_dir / "session.json"
    with path.open("w", encoding="utf-8") as session_file:
        json.dump(payload, session_file, ensure_ascii=False, indent=2)
        session_file.write("\n")
        session_file.flush()


def _percentile(ordered: list[float], percentile: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def run_probe(options: ProbeOptions) -> int:
    """Run the foreground-only probe until Ctrl+C or a human-readable failure."""
    archive = CaptureArchive(options.save_dir)
    detector = FrameChangeDetector(threshold=options.threshold)
    tracker = FrameComparisonTracker(detector)
    policy = SavePolicy(
        options.strategy,
        options.threshold,
        options.min_save_interval,
        options.max_silence,
    )
    probe_statistics = ProbeStatistics()
    backend: WindowsGraphicsCaptureBackend | None = None
    csv_writer = MetricsCsvWriter(options.save_dir)
    exit_code = 0
    started_at = datetime.now(timezone.utc)
    _write_session(options, started_at, None, 0, None)
    _print_banner(options)
    try:
        if options.title is None:
            print(
                f"未指定 --title：{FOREGROUND_SELECTION_DELAY_SECONDS} 秒后锁定前台窗口，"
                "请现在切回游戏。"
            )
            time.sleep(FOREGROUND_SELECTION_DELAY_SECONDS)
        backend = WindowsGraphicsCaptureBackend(options.title)
        print(
            f"目标：{backend.target.title} | {backend.target.process_name} | "
            f"阈值 {options.threshold:.5f} | 间隔 {options.interval:.2f}s"
        )
        consecutive_unavailable = 0
        while True:
            iteration_started = time.perf_counter()
            capture_started = time.perf_counter()
            frame = backend.capture_frame()
            capture_duration = time.perf_counter() - capture_started
            probe_statistics.capture_durations.append(capture_duration)
            probe_statistics.poll_count += 1
            if frame is None:
                probe_statistics.unavailable_count += 1
                consecutive_unavailable += 1
                csv_writer.write_unavailable(
                    probe_statistics.poll_count,
                    datetime.now(timezone.utc),
                    backend.target.title,
                    capture_duration * 1000,
                )
                if consecutive_unavailable == 1:
                    print("WGC 暂无新帧；本次已记录并继续轮询，不会阻塞等待")
                elapsed = time.perf_counter() - iteration_started
                time.sleep(max(0.0, options.interval - elapsed))
                continue
            if consecutive_unavailable:
                print(f"WGC 已恢复交帧；此前连续 {consecutive_unavailable} 次无新帧")
                consecutive_unavailable = 0
            probe_statistics.captured_count += 1
            probe_statistics.source_frames_drained += (
                frame.metadata.source_frames_drained
            )
            probe_statistics.max_source_frames_drained = max(
                probe_statistics.max_source_frames_drained,
                frame.metadata.source_frames_drained,
            )

            metrics_started = time.perf_counter()
            observation = tracker.observe(frame.bitmap)
            metrics_duration_ms = (time.perf_counter() - metrics_started) * 1000
            probe_statistics.record(observation.comparisons, metrics_duration_ms)
            decision_time = time.monotonic()
            decision = policy.decide(
                observation.comparisons,
                decision_time,
                is_first=observation.is_first,
            )
            saved_filename = ""
            if decision.should_save:
                saved_path = archive.save(frame, probe_statistics.poll_count)
                probe_statistics.saved_count += 1
                probe_statistics.forced_saved_count += int(decision.forced)
                policy.mark_saved(decision_time)
                if not decision.forced:
                    # A silence safeguard is archived, but it was not judged "changed".
                    tracker.mark_saved(observation.prepared)
                local_time = frame.metadata.captured_at.astimezone().strftime("%H:%M:%S")
                value = observation.comparisons.strategy_value(options.strategy)
                difference_text = "首帧" if observation.is_first else f"{value:.5f}"
                retained = saved_path.name if saved_path is not None else "已因容量上限淘汰"
                saved_filename = "" if saved_path is None else saved_path.name
                print(
                    f"{local_time} | {frame.metadata.window_title} | "
                    f"{options.strategy}={difference_text} | forced="
                    f"{'true' if decision.forced else 'false'} | {retained}"
                )
            csv_writer.write(
                probe_statistics.poll_count,
                frame.metadata.captured_at,
                frame.metadata.window_title,
                observation.comparisons,
                decision.should_save,
                decision.forced,
                saved_filename,
                metrics_duration_ms,
                source_frames_drained=frame.metadata.source_frames_drained,
                capture_duration_ms=capture_duration * 1000,
            )
            elapsed = time.perf_counter() - iteration_started
            time.sleep(max(0.0, options.interval - elapsed))
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在干净退出……")
    except CaptureError as error:
        print(f"\n截屏停止：{error}", file=sys.stderr)
        exit_code = 1
    finally:
        if backend is not None:
            backend.close()
        csv_writer.close()
        summary = _build_summary(probe_statistics, archive)
        _print_summary(summary)
        _write_session(
            options,
            started_at,
            datetime.now(timezone.utc),
            probe_statistics.poll_count,
            summary,
        )
    return exit_code


def main() -> None:
    _configure_console_encoding()
    parser = _build_parser()
    arguments = parser.parse_args()
    if not arguments.watch:
        parser.error("请使用 --watch 启动前台探针")
    options = ProbeOptions(
        interval=arguments.interval,
        threshold=arguments.threshold,
        title=arguments.title,
        save_dir=arguments.save_dir or _default_save_dir(),
        strategy=arguments.strategy,
        min_save_interval=arguments.min_save_interval,
        max_silence=arguments.max_silence,
        label=arguments.label,
    )
    raise SystemExit(run_probe(options))


if __name__ == "__main__":
    main()
