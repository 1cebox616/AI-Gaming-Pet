"""Single-window capture and a local frame-change probe.

The production-facing classes in this module never select a monitor or the
desktop.  A Windows HWND is resolved first, then passed to Windows Graphics
Capture through the optional ``zbl`` binding.
"""

from __future__ import annotations

import argparse
from collections import deque
import csv
import ctypes
import importlib
import io
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from types import ModuleType
from typing import Literal, Protocol, Self, Sequence

import numpy as np
import numpy.typing as npt
from PIL import Image

from pet.core.input_telemetry import (
    ClockAnchor,
    INPUT_WHITELIST_VERSION,
    KEYBOARD_INPUT_NAMES,
    MOUSE_INPUT_NAMES,
    FullKeyboardWindowsRawInputBackend,
    InputTelemetryError,
    InputTelemetryRecorder,
    WindowsRawInputBackend,
    empty_input_summary,
)

DEFAULT_INTERVAL_SECONDS = 1.0
DEFAULT_THRESHOLD = 0.02
DEFAULT_CHANGE_WIDTH = 96
# 此数待实测确定。
DEFAULT_AREA_WIDTH = 320
# 此数待实测确定。
DEFAULT_PIXEL_DELTA_THRESHOLD = 24
# 此数待实测确定。
DEFAULT_BLOCK_GRID = (9, 16)  # 9 列 × 16 行。
# 此数待实测确定。
DEFAULT_BLOCK_DELTA_THRESHOLD = 12
DEFAULT_REGION_SPARSITY_MAX = 0.25
DEFAULT_MIN_SAVE_INTERVAL_SECONDS = 1.0
DEFAULT_MAX_SILENCE_SECONDS = 60.0
DEFAULT_MAX_FILES = 500
DEFAULT_MAX_BYTES = 200 * 1024 * 1024
# 设计原则：本地帧选取的首要目标是不漏掉应上传的帧，次要目标才是丢弃冗余帧。
# M5-T6 second-round constrained selection fixed these production defaults.
# Versus N=20/k=2.5/margin=4/255, N=10/k=1.2/margin=4/255 changed
# subtitle strong misses 3→0 and world-event hits 14→19 while retaining a
# 2.0% upload rate on the rain-idle negative anchor.
DEFAULT_NOISE_WINDOW = 10
DEFAULT_NOISE_MULTIPLIER = 1.2
DEFAULT_NOISE_MARGIN = 4.0 / 255.0
# M5-T4 的 1403 帧实测中，P=2 相比 P=1 将局部剧变漏检从
# 11.6 提高到 115.9、输入活动漏检从 49.0 提高到 153.3，因此默认取 1。
DEFAULT_PERSISTENCE_POLLS = 1
# Imported from core.config so capture and the generic adapter use one value.
# M5-T4 calibration proxies. These initial values are awaiting calibration too;
# they measure misses and do not alter the production selector's decision.
DEFAULT_STRONG_BLOCK_DELTA = 40.0 / 255.0
DEFAULT_INPUT_MOTION_THRESHOLD = 20.0
# 640 is below the production upload width, so recordings at that width cannot
# support text-recognition comparisons.  Retain full-HD probe material instead.
DEFAULT_RAW_WIDTH = 1920
DEFAULT_RAW_JPEG_QUALITY = 70
DEFAULT_RAW_MAX_FILES = 5_000
DEFAULT_RAW_MAX_BYTES = 4096 * 1024 * 1024
# Measured on the four M5-T8 full-HD captures at JPEG quality 70.  This is only
# a startup planning estimate; scene complexity determines the actual rate.
ESTIMATED_RAW_JPEG_BYTES_PER_FRAME = 170_000
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
    monotonic_seconds: float
    time_source: Literal["presentation", "capture"]
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

    def __init__(
        self,
        title_filter: str | None = None,
        *,
        capture_cursor: bool = False,
    ) -> None:
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
                is_cursor_capture_enabled=capture_cursor,
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
        # zbl 0.7.1 does not expose WGC's SystemRelativeTime on its Python
        # Frame object.  This near-simultaneous pair therefore timestamps the
        # owned bitmap at capture completion; presentation-time calibration is
        # intentionally not fabricated.
        captured_anchor = ClockAnchor.sample()
        user32 = _configured_user32()
        current_target = _target_from_hwnd(self._target.hwnd, user32)
        self._target = current_target
        return CapturedFrame(
            bitmap=bitmap,
            metadata=FrameMetadata(
                window_title=current_target.title,
                process_name=current_target.process_name,
                captured_at=captured_anchor.utc,
                monotonic_seconds=captured_anchor.monotonic_seconds,
                time_source="capture",
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
        block_differences = self.block_mean_differences(reference, current)
        changed_blocks = int(
            np.count_nonzero(block_differences * 255.0 > self.block_delta_threshold)
        )
        columns, rows = self.block_grid
        return FrameMetrics(
            mean_amplitude=float(mean_difference.mean() / 255.0),
            changed_area=changed_area,
            block_change=changed_blocks / (columns * rows),
        )

    def block_mean_differences(
        self,
        reference: PreparedFrame,
        current: PreparedFrame,
    ) -> npt.NDArray[np.float32]:
        """Return normalized mean absolute differences for the 16×9 grid.

        This calculation is pure. Rolling noise history and persistence live in
        ``AdaptiveFrameSelector`` so synthetic images can exercise it without a
        capture backend.
        """
        reference_area, current_area = _align_gray_arrays(
            reference.area_gray, current.area_gray
        )
        difference = np.abs(reference_area - current_area)
        columns, rows = self.block_grid
        values = [
            float(block.mean() / 255.0)
            for row in np.array_split(difference, rows, axis=0)
            for block in np.array_split(row, columns, axis=1)
        ]
        return np.asarray(values, dtype=np.float32).reshape(rows, columns)


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

    def save(
        self, frame: CapturedFrame, sequence: int, *, suffix: str = ""
    ) -> Path | None:
        timestamp = frame.metadata.captured_at.astimezone(timezone.utc).strftime(
            "%Y%m%dT%H%M%S.%fZ"
        )
        path = self.save_dir / f"frame-{sequence:06d}-{timestamp}{suffix}.png"
        frame.bitmap.save(path, format="PNG")
        stat = path.stat()
        self._entries.append(
            ArchivedFrame(path=path, size_bytes=stat.st_size, modified_ns=stat.st_mtime_ns)
        )
        self._entries.sort(key=lambda entry: (entry.modified_ns, entry.path.name))
        self._enforce_limits()
        return path if path.exists() else None

    def save_pair(
        self,
        current: CapturedFrame,
        previous: CapturedFrame | None,
        sequence: int,
    ) -> tuple[Path | None, Path | None]:
        """Save the selected frame and its immediately preceding poll."""
        previous_path = (
            None
            if previous is None
            else self.save(previous, sequence, suffix="-prev")
        )
        current_path = self.save(current, sequence)
        return current_path, previous_path

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


class RawFrameArchive:
    """Retain a replayable, bounded stream of downscaled JPEG polling frames."""

    def __init__(
        self,
        raw_dir: Path,
        *,
        width: int = DEFAULT_RAW_WIDTH,
        jpeg_quality: int = DEFAULT_RAW_JPEG_QUALITY,
        max_files: int = DEFAULT_RAW_MAX_FILES,
        max_bytes: int = DEFAULT_RAW_MAX_BYTES,
    ) -> None:
        if width <= 0:
            raise ValueError("raw width must be positive")
        if not 1 <= jpeg_quality <= 95:
            raise ValueError("JPEG quality must be between 1 and 95")
        if max_files <= 0 or max_bytes <= 0:
            raise ValueError("raw archive limits must be positive")
        self.raw_dir = raw_dir
        self.width = width
        self.jpeg_quality = jpeg_quality
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._entries = self._existing_jpegs()
        self._enforce_limits()

    @property
    def retained_count(self) -> int:
        return len(self._entries)

    @property
    def retained_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self._entries)

    def save(self, frame: CapturedFrame, sequence: int) -> tuple[Path | None, Image.Image]:
        """Save one poll and return the exact decoded bitmap replay will consume."""
        image = frame.bitmap.convert("RGB")
        target_height = max(1, round(image.height * self.width / image.width))
        image = image.resize((self.width, target_height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=self.jpeg_quality)
        payload = buffer.getvalue()
        timestamp = frame.metadata.captured_at.astimezone(timezone.utc).strftime(
            "%Y%m%dT%H%M%S.%fZ"
        )
        path = self.raw_dir / f"raw-{sequence:06d}-{timestamp}.jpg"
        path.write_bytes(payload)
        with Image.open(io.BytesIO(payload)) as decoded:
            replay_bitmap = decoded.convert("RGB")
        stat = path.stat()
        self._entries.append(ArchivedFrame(path, stat.st_size, stat.st_mtime_ns))
        self._entries.sort(key=lambda entry: (entry.modified_ns, entry.path.name))
        self._enforce_limits()
        return (path if path.exists() else None), replay_bitmap

    def _existing_jpegs(self) -> list[ArchivedFrame]:
        entries: list[ArchivedFrame] = []
        for path in self.raw_dir.glob("*.jpg"):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            entries.append(ArchivedFrame(path, stat.st_size, stat.st_mtime_ns))
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
    decision_reason_counts: dict[DecisionReason, int] = field(
        default_factory=lambda: {reason: 0 for reason in DECISION_REASONS}
    )
    nonempty_region_ratios: list[float] = field(default_factory=list)
    region_sparsity_cleared_count: int = 0

    def record(self, observation: SelectionObservation, duration_ms: float) -> None:
        self.metric_durations_ms.append(duration_ms)
        for strategy in STRATEGY_NAMES:
            getattr(self, strategy).append(
                observation.comparisons.strategy_value(strategy)
            )
        decision = observation.decision
        self.decision_reason_counts[decision.reason] += 1
        if decision.should_save:
            self.saved_count += 1
            self.forced_saved_count += int(decision.forced)
            if decision.region_grid:
                self.nonempty_region_ratios.append(decision.changed_block_ratio)
            if decision.region_sparsity_suppressed:
                self.region_sparsity_cleared_count += 1

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


DecisionReason = Literal[
    "persistent_change",
    "forced",
    "suppressed_min_interval",
    "no_change",
]
DECISION_REASONS: tuple[DecisionReason, ...] = (
    "persistent_change",
    "forced",
    "suppressed_min_interval",
    "no_change",
)


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    """Adaptive block decision after persistence and timing safeguards."""

    should_save: bool
    forced: bool
    reason: DecisionReason
    changed_block_count: int
    changed_block_ratio: float
    region_grid: tuple[str, ...]
    confirmed_region_grid: tuple[str, ...]
    region_sparsity_suppressed: bool
    floor_median: float
    baseline_monotonic_seconds: float = field(compare=False)
    confirmed_region_intensity: float = field(default=0.0, compare=False)


@dataclass(frozen=True, slots=True)
class SelectionObservation:
    """One prepared frame, six legacy metrics, and the adaptive decision."""

    prepared: PreparedFrame
    comparisons: FrameComparisons
    decision: SelectionDecision
    is_first: bool


class AdaptiveFrameSelector:
    """Stateful policy around the stateless ``FrameChangeDetector``.

    The last selected change frame is the comparison baseline. Persistent rain,
    waves, and foliage raise each block's rolling noise floor independently.
    Selection prioritizes not missing upload-worthy frames over eliminating every
    redundant frame.
    """

    def __init__(
        self,
        detector: FrameChangeDetector | None = None,
        *,
        noise_window: int = DEFAULT_NOISE_WINDOW,
        noise_multiplier: float = DEFAULT_NOISE_MULTIPLIER,
        noise_margin: float = DEFAULT_NOISE_MARGIN,
        persistence_polls: int = DEFAULT_PERSISTENCE_POLLS,
        region_sparsity_max: float = DEFAULT_REGION_SPARSITY_MAX,
        min_save_interval: float = DEFAULT_MIN_SAVE_INTERVAL_SECONDS,
        max_silence: float = DEFAULT_MAX_SILENCE_SECONDS,
    ) -> None:
        self.detector = detector or FrameChangeDetector()
        if noise_window <= 0:
            raise ValueError("noise_window must be positive")
        if noise_multiplier < 0.0:
            raise ValueError("noise_multiplier must not be negative")
        if not 0.0 <= noise_margin <= 1.0:
            raise ValueError("noise_margin must be between 0 and 1")
        if persistence_polls <= 0:
            raise ValueError("persistence_polls must be positive")
        if not 0.0 <= region_sparsity_max <= 1.0:
            raise ValueError("region_sparsity_max must be between 0 and 1")
        if min_save_interval < 0.0:
            raise ValueError("min_save_interval must not be negative")
        if max_silence <= 0.0:
            raise ValueError("max_silence must be positive")
        columns, rows = self.detector.block_grid
        self.noise_window = noise_window
        self.noise_multiplier = noise_multiplier
        self.noise_margin = noise_margin
        self.persistence_polls = persistence_polls
        self.region_sparsity_max = region_sparsity_max
        self.min_save_interval = min_save_interval
        self.max_silence = max_silence
        self._histories = [
            deque(maxlen=noise_window) for _ in range(columns * rows)
        ]
        self._consecutive = np.zeros((rows, columns), dtype=np.int32)
        self._streak_floors = np.full(
            (rows, columns), noise_margin, dtype=np.float32
        )
        self._previous: PreparedFrame | None = None
        self._baseline: PreparedFrame | None = None
        self._baseline_at: float | None = None
        self._last_saved_at: float | None = None

    def observe(self, frame: FrameLike, now: float) -> SelectionObservation:
        prepared = self.detector.prepare(frame)
        is_first = self._previous is None
        previous = self._previous or prepared
        baseline = self._baseline or prepared
        comparisons = FrameComparisons(
            vs_previous=self.detector.compare_prepared(previous, prepared),
            vs_baseline=self.detector.compare_prepared(baseline, prepared),
        )
        decision = self.observe_prepared(prepared, now)
        return SelectionObservation(prepared, comparisons, decision, is_first)

    def observe_prepared(
        self, prepared: PreparedFrame, now: float
    ) -> SelectionDecision:
        """Select one already-reduced frame with the exact online state machine.

        Calibration reuses the same decoded frame across hundreds of parameter
        combinations. Reusing the grayscale reductions changes no selector
        logic and avoids repeatedly resizing the same read-only recording.
        """
        is_first = self._previous is None
        baseline = self._baseline or prepared
        baseline_at = self._baseline_at if self._baseline_at is not None else now
        floors = np.asarray(
            [
                (statistics.median(history) * self.noise_multiplier)
                + self.noise_margin
                if history
                else self.noise_margin
                for history in self._histories
            ],
            dtype=np.float32,
        ).reshape(self._consecutive.shape)
        differences = self.detector.block_mean_differences(baseline, prepared)
        # Freeze a block's floor for the duration of one over-floor streak. An
        # event's first high sample may enter rolling history, but cannot raise
        # its own threshold before persistence has a chance to confirm it.
        active_streak = self._consecutive > 0
        thresholds = np.where(active_streak, self._streak_floors, floors)
        above_floor = differences > thresholds
        starting_streak = above_floor & ~active_streak
        self._streak_floors[starting_streak] = floors[starting_streak]
        self._streak_floors[~above_floor] = floors[~above_floor]
        self._consecutive = np.where(above_floor, self._consecutive + 1, 0)
        confirmed = self._consecutive >= self.persistence_polls
        for history, value in zip(self._histories, differences.flat, strict=True):
            history.append(float(value))

        changed_count = int(np.count_nonzero(confirmed))
        changed_ratio = changed_count / confirmed.size
        confirmed_region_intensity = (
            float(np.mean(differences[confirmed])) if changed_count else 0.0
        )
        confirmed_region_grid = tuple(
            f"r{row + 1}c{column + 1}"
            for row, column in np.argwhere(confirmed)
        )
        if changed_count:
            candidate_reason: DecisionReason = "persistent_change"
            region_sparsity_suppressed = changed_ratio > self.region_sparsity_max
            region_grid = (
                ()
                if region_sparsity_suppressed
                else confirmed_region_grid
            )
        else:
            candidate_reason = "no_change"
            region_grid = ()
            region_sparsity_suppressed = False

        if is_first or self._last_saved_at is None:
            # The first frame establishes a baseline. ``forced`` is the closest
            # closed-set reason because no comparison can exist yet.
            decision = SelectionDecision(
                True,
                True,
                "forced",
                0,
                0.0,
                (),
                (),
                False,
                float(np.median(floors)),
                baseline_at,
                0.0,
            )
            self._baseline = prepared
            self._baseline_at = now
            self._last_saved_at = now
            self._consecutive.fill(0)
        else:
            since_saved = now - self._last_saved_at
            if since_saved >= self.max_silence:
                decision = SelectionDecision(
                    True,
                    True,
                    "forced",
                    changed_count,
                    changed_ratio,
                    region_grid,
                    confirmed_region_grid,
                    region_sparsity_suppressed,
                    float(np.median(floors)),
                    baseline_at,
                    confirmed_region_intensity,
                )
                self._last_saved_at = now
            elif candidate_reason != "no_change" and since_saved < self.min_save_interval:
                decision = SelectionDecision(
                    False,
                    False,
                    "suppressed_min_interval",
                    changed_count,
                    changed_ratio,
                    region_grid,
                    confirmed_region_grid,
                    region_sparsity_suppressed,
                    float(np.median(floors)),
                    baseline_at,
                    confirmed_region_intensity,
                )
            elif candidate_reason != "no_change":
                decision = SelectionDecision(
                    True,
                    False,
                    candidate_reason,
                    changed_count,
                    changed_ratio,
                    region_grid,
                    confirmed_region_grid,
                    region_sparsity_suppressed,
                    float(np.median(floors)),
                    baseline_at,
                    confirmed_region_intensity,
                )
                self._baseline = prepared
                self._baseline_at = now
                self._last_saved_at = now
                self._consecutive.fill(0)
            else:
                decision = SelectionDecision(
                    False,
                    False,
                    "no_change",
                    0,
                    0.0,
                    (),
                    (),
                    False,
                    float(np.median(floors)),
                    baseline_at,
                    0.0,
                )
        self._previous = prepared
        return decision


CSV_HEADER = (
    "序号",
    "时间",
    "单调秒",
    "时间来源",
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
    "本次前一帧文件名",
    "确实变了的块数",
    "confirmed块占比",
    "是否因超过稀疏上限而清空region_grid",
    "变化格子",
    "每块噪声地板中位值",
    "判定原因",
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
        observation: SelectionObservation,
        saved_filename: str,
        previous_filename: str,
        duration_ms: float,
        *,
        monotonic_seconds: float | None = None,
        time_source: Literal["presentation", "capture"] = "capture",
        source_frames_drained: int = 1,
        capture_duration_ms: float = 0.0,
    ) -> None:
        previous = observation.comparisons.vs_previous
        baseline = observation.comparisons.vs_baseline
        decision = observation.decision
        self._writer.writerow(
            (
                sequence,
                captured_at.isoformat(),
                f"{(captured_at.timestamp() if monotonic_seconds is None else monotonic_seconds):.9f}",
                time_source,
                window_title,
                f"{previous.mean_amplitude:.9f}",
                f"{previous.changed_area:.9f}",
                f"{previous.block_change:.9f}",
                f"{baseline.mean_amplitude:.9f}",
                f"{baseline.changed_area:.9f}",
                f"{baseline.block_change:.9f}",
                "是" if decision.should_save else "否",
                "是" if decision.forced else "否",
                saved_filename,
                previous_filename,
                decision.changed_block_count,
                f"{decision.changed_block_ratio:.9f}",
                "是" if decision.region_sparsity_suppressed else "否",
                "、".join(decision.region_grid),
                f"{decision.floor_median:.9f}",
                decision.reason,
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
        *,
        monotonic_seconds: float | None = None,
    ) -> None:
        """Record and flush a poll where WGC had no frame ready."""
        self._writer.writerow(
            (
                sequence,
                attempted_at.isoformat(),
                f"{(attempted_at.timestamp() if monotonic_seconds is None else monotonic_seconds):.9f}",
                "capture",
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
                "",
                "",
                "",
                "",
                "",
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
    """Validated command-line options shared by watch and offline replay."""

    interval: float
    title: str | None
    save_dir: Path
    min_save_interval: float = DEFAULT_MIN_SAVE_INTERVAL_SECONDS
    max_silence: float = DEFAULT_MAX_SILENCE_SECONDS
    noise_window: int = DEFAULT_NOISE_WINDOW
    noise_multiplier: float = DEFAULT_NOISE_MULTIPLIER
    noise_margin: float = DEFAULT_NOISE_MARGIN
    persistence_polls: int = DEFAULT_PERSISTENCE_POLLS
    region_sparsity_max: float = DEFAULT_REGION_SPARSITY_MAX
    label: str | None = None
    capture_cursor: bool = False
    record_all: bool = False
    record_input: bool = False
    input_full_keyboard: bool = False
    raw_width: int = DEFAULT_RAW_WIDTH
    raw_max_files: int = DEFAULT_RAW_MAX_FILES
    raw_max_bytes: int = DEFAULT_RAW_MAX_BYTES
    replay_dir: Path | None = None
    segments_path: Path | None = None
    segment_role: str | None = None


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


def _unit_interval(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("必须在 0 到 1 之间")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return parsed


def _default_save_dir() -> Path:
    backend_root = Path(__file__).resolve().parents[3]
    started_at = datetime.now().strftime("%Y%m%d-%H%M%S")
    return backend_root / "recordings" / "capture" / started_at


def _default_calibration_dir() -> Path:
    backend_root = Path(__file__).resolve().parents[3]
    started_at = datetime.now().strftime("%Y%m%d-%H%M%S")
    return backend_root / "eval-reports" / f"calibration-{started_at}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只抓一个游戏窗口的本地画面变化探针")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--watch", action="store_true", help="持续抓帧，Ctrl+C 停止")
    mode.add_argument("--replay", type=Path, metavar="RAW目录", help="离线重放 raw JPEG 流")
    mode.add_argument(
        "--calibrate",
        nargs="*",
        metavar="角色=会话目录",
        help="在会话参数和/或 --segments 区间上离线搜索检测参数",
    )
    parser.add_argument(
        "--interval",
        type=_positive_float,
        default=DEFAULT_INTERVAL_SECONDS,
        help="相邻采样间隔秒数（默认 1.0）",
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
    parser.add_argument(
        "--noise-window",
        type=_positive_int,
        default=DEFAULT_NOISE_WINDOW,
        help="每块噪声地板的滚动窗口（默认 20，待实测）",
    )
    parser.add_argument(
        "--noise-multiplier",
        type=_nonnegative_float,
        default=DEFAULT_NOISE_MULTIPLIER,
        help="滚动中位数乘数 k（默认 2.5，待实测）",
    )
    parser.add_argument(
        "--noise-margin",
        type=_unit_interval,
        default=DEFAULT_NOISE_MARGIN,
        help="噪声地板固定余量 0..1（默认 4/255，待实测）",
    )
    parser.add_argument(
        "--persistence-polls",
        type=_positive_int,
        default=DEFAULT_PERSISTENCE_POLLS,
        help="连续超地板的轮询次数（默认 1，M5-T4 实测定案）",
    )
    parser.add_argument(
        "--region-sparsity-max",
        type=_unit_interval,
        default=DEFAULT_REGION_SPARSITY_MAX,
        help="保留 region_grid 的最大 confirmed 块占比（默认 0.25，待与 M5-A 扫描统一）",
    )
    parser.add_argument("--label", help="本次采集会话的可选标签")
    parser.add_argument("--title", help="目标窗口标题中的一段文字")
    parser.add_argument(
        "--capture-cursor",
        action="store_true",
        help="把鼠标光标合成进 WGC 画面（默认关闭；用于光标兼容性 A/B）",
    )
    parser.add_argument(
        "--save-dir",
        type=Path,
        help="输出目录（默认 backend/recordings/capture/<启动时间>/）",
    )
    parser.add_argument(
        "--record-all",
        action="store_true",
        help="把每次成功轮询都另存为可重放 JPEG",
    )
    parser.add_argument(
        "--record-input",
        action="store_true",
        help="被动记录固定白名单内的 Raw Input 键鼠事件（默认关闭）",
    )
    parser.add_argument(
        "--input-full-keyboard",
        action="store_true",
        help="开发期记录全键盘真实键名；可能包含聊天与输入框内容（默认关闭）",
    )
    parser.add_argument(
        "--raw-width",
        type=_positive_int,
        default=DEFAULT_RAW_WIDTH,
        help="全量流 JPEG 宽度（默认 640）",
    )
    parser.add_argument(
        "--raw-max-files",
        type=_positive_int,
        default=DEFAULT_RAW_MAX_FILES,
        help="全量流最多保留文件数（默认 5000）",
    )
    parser.add_argument(
        "--raw-max-bytes",
        type=_positive_int,
        default=DEFAULT_RAW_MAX_BYTES,
        help="全量流最大总字节数（默认 1073741824）",
    )
    parser.add_argument("--grid", type=Path, help="校准搜索空间 TOML（默认使用内置 108 组）")
    parser.add_argument(
        "--segments",
        type=Path,
        help="只读区间 TOML；校准可含多段，重放须只匹配一段",
    )
    parser.add_argument(
        "--segment-role",
        help="--replay 使用多区间 TOML 时选择一个角色",
    )
    parser.add_argument(
        "--sample-stride",
        type=_positive_int,
        default=1,
        help="校准时每隔多少张 raw 帧取一张（默认 1，即不抽样）",
    )
    parser.add_argument(
        "--strong-block-delta",
        type=_unit_interval,
        default=DEFAULT_STRONG_BLOCK_DELTA,
        help="局部剧变块阈值 0..1（默认 40/255，待校准）",
    )
    parser.add_argument(
        "--input-motion-threshold",
        type=_nonnegative_float,
        default=DEFAULT_INPUT_MOTION_THRESHOLD,
        help="轮询窗口鼠标位移阈值（默认 20 像素，待校准）",
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
    print(f"自适应块检测：16 行 × 9 列；持久性 {options.persistence_polls} 轮")
    print(
        f"噪声地板：最近 {options.noise_window} 轮中位数 × "
        f"{options.noise_multiplier:g} + {options.noise_margin:.6f}；"
        f"区域稀疏上限：{options.region_sparsity_max:.3f}"
    )
    print(
        f"最短落盘间隔：{options.min_save_interval:.1f}s；"
        f"最长静默：{options.max_silence:.1f}s"
    )
    print(f"WGC 光标合成：{'开启' if options.capture_cursor else '关闭'}")
    if options.record_all:
        estimated_mib_per_hour = (
            ESTIMATED_RAW_JPEG_BYTES_PER_FRAME
            * 3600.0
            / max(options.interval, 1e-9)
            / (1024 * 1024)
        )
        print(
            "【全量录制已开启】每次成功轮询都会保存到 raw/："
            f"宽 {options.raw_width}px、JPEG 质量 {DEFAULT_RAW_JPEG_QUALITY}"
        )
        print(
            f"预计磁盘占用速率：约 {estimated_mib_per_hour:.0f} MiB/小时"
            "（按 1080p 实测均值估算，实际随画面复杂度变化）"
        )
    if options.record_input:
        print("【键鼠输入记录已开启】仅在目标游戏窗口位于前台时记录")
        if options.input_full_keyboard:
            print("【警告：全键盘记录已开启】会包含聊天与输入框内容")
            print("全键盘数据只存本地会话目录；请勿提交 input.csv 或 session.json")
        else:
            print(
                f"键盘白名单 {INPUT_WHITELIST_VERSION}："
                + "、".join(KEYBOARD_INPUT_NAMES)
            )
        print(
            f"鼠标白名单 {INPUT_WHITELIST_VERSION}："
            + "、".join(MOUSE_INPUT_NAMES)
            + "；移动只记相对 dx/dy 绝对值的 100ms 汇总，绝不记录绝对坐标"
        )
        print(f"输入记录位置：{options.save_dir / 'input.csv'}")
    print("首帧、持久变化及最长静默强制帧落盘；Ctrl+C 停止")
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
    statistics_: ProbeStatistics,
    archive: CaptureArchive | None,
    raw_archive: RawFrameArchive | None = None,
) -> dict[str, object]:
    metrics = {
        strategy: _distribution(values)
        for strategy, values in statistics_.metric_series()
    }
    metric_timing = _distribution(statistics_.metric_durations_ms)
    captured = statistics_.captured_count
    reason_summary = {
        reason: {
            "次数": count,
            "占比": 0.0 if captured == 0 else count / captured,
        }
        for reason, count in statistics_.decision_reason_counts.items()
    }
    return {
        "总轮询数": statistics_.poll_count,
        "成功抓帧数": statistics_.captured_count,
        "无新帧轮询数": statistics_.unavailable_count,
        "累计取出源帧数": statistics_.source_frames_drained,
        "单次最多取出源帧数": statistics_.max_source_frames_drained,
        "落盘次数": statistics_.saved_count,
        "强制落盘次数": statistics_.forced_saved_count,
        "当前保留张数": 0 if archive is None else archive.retained_count,
        "当前保留字节数": 0 if archive is None else archive.retained_bytes,
        "raw保留张数": 0 if raw_archive is None else raw_archive.retained_count,
        "raw保留字节数": 0 if raw_archive is None else raw_archive.retained_bytes,
        "上传率": 0.0 if captured == 0 else statistics_.saved_count / captured,
        "判定原因": reason_summary,
        "region_grid非空比例": 0.0
        if statistics_.saved_count == 0
        else len(statistics_.nonempty_region_ratios) / statistics_.saved_count,
        "因稀疏上限清空region_grid次数": statistics_.region_sparsity_cleared_count,
        "区域格子非空时平均占比": None
        if not statistics_.nonempty_region_ratios
        else statistics.mean(statistics_.nonempty_region_ratios),
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
    print(f"上传率：{float(summary['上传率']):.2%}")
    reason_summaries = summary["判定原因"]
    assert isinstance(reason_summaries, dict)
    for reason in DECISION_REASONS:
        values = reason_summaries[reason]
        assert isinstance(values, dict)
        print(f"{reason}：{values['次数']} 次 / {float(values['占比']):.2%}")
    average_region_ratio = summary["区域格子非空时平均占比"]
    print(f"region_grid 非空比例：{float(summary['region_grid非空比例']):.2%}")
    print(f"因稀疏上限清空 region_grid：{summary['因稀疏上限清空region_grid次数']} 次")
    if isinstance(average_region_ratio, float):
        print(f"region_grid 非空时平均格子占比：{average_region_ratio:.2%}")
    if int(summary["raw保留张数"]):
        print(
            f"raw 全量流：{summary['raw保留张数']} 张 / "
            f"{int(summary['raw保留字节数']) / (1024 * 1024):.2f} MB"
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


def _print_input_summary(summary: dict[str, object]) -> None:
    print("\n键鼠输入记录汇总")
    print(
        f"白名单版本：{summary['白名单版本']}；"
        f"事件总数：{summary['事件总数']}；"
        f"focus_lost：{summary['focus_lost次数']} 次"
    )
    print(f"按键名分组的按下次数：{summary['按键名分组的按下次数']}")
    median = summary["鼠标位移量中位"]
    p90 = summary["鼠标位移量P90"]
    if isinstance(median, (int, float)) and isinstance(p90, (int, float)):
        print(f"每 100ms 鼠标位移量：中位 {median:.2f} / P90 {p90:.2f}")
    else:
        print("每 100ms 鼠标位移量：无样本")


def _write_session(
    options: ProbeOptions,
    started_at: datetime,
    ended_at: datetime | None,
    total_polls: int,
    summary: dict[str, object] | None,
    input_summary: dict[str, object] | None = None,
    *,
    clock_anchor: ClockAnchor | None = None,
) -> None:
    anchor = clock_anchor or ClockAnchor(started_at, started_at.timestamp())
    input_payload = dict(input_summary or empty_input_summary())
    input_payload["已开启"] = options.record_input
    input_payload["全键盘模式"] = options.input_full_keyboard
    payload = {
        "标签": options.label,
        "启动参数": {
            "mode": "replay" if options.replay_dir is not None else "watch",
            "interval": options.interval,
            "title": options.title,
            "save_dir": str(options.save_dir),
            "replay_dir": None
            if options.replay_dir is None
            else str(options.replay_dir),
            "min_save_interval": options.min_save_interval,
            "max_silence": options.max_silence,
            "noise_window": options.noise_window,
            "noise_multiplier": options.noise_multiplier,
            "noise_margin": options.noise_margin,
            "persistence_polls": options.persistence_polls,
            "region_sparsity_max": options.region_sparsity_max,
            "capture_cursor": options.capture_cursor,
            "record_all": options.record_all,
            "record_input": options.record_input,
            "input_full_keyboard": options.input_full_keyboard,
            "raw_width": options.raw_width,
            "raw_max_files": options.raw_max_files,
            "raw_max_bytes": options.raw_max_bytes,
            "raw_jpeg_quality": DEFAULT_RAW_JPEG_QUALITY,
        },
        "开始时间": started_at.isoformat(),
        "结束时间": None if ended_at is None else ended_at.isoformat(),
        "总轮询数": total_polls,
        "汇总": summary,
        "输入记录": input_payload,
        "时间基准": "同一进程 time.perf_counter 单调秒；UTC 墙钟仅供人读",
        "时钟锚点": {
            "perf_counter秒": anchor.monotonic_seconds,
            "对应UTC墙钟": anchor.utc.isoformat(),
        },
        "帧时间戳": {
            "来源": "capture",
            "呈现时间换算常数": None,
            "说明": "zbl 0.7.1 未向 Python 暴露 WGC SystemRelativeTime；使用抓取完成时刻",
        },
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


def _make_selector(options: ProbeOptions) -> AdaptiveFrameSelector:
    return AdaptiveFrameSelector(
        noise_window=options.noise_window,
        noise_multiplier=options.noise_multiplier,
        noise_margin=options.noise_margin,
        persistence_polls=options.persistence_polls,
        region_sparsity_max=options.region_sparsity_max,
        min_save_interval=options.min_save_interval,
        max_silence=options.max_silence,
    )


def run_probe(options: ProbeOptions) -> int:
    """Run the foreground-only probe until Ctrl+C or a human-readable failure."""
    archive = CaptureArchive(options.save_dir)
    raw_archive = (
        RawFrameArchive(
            options.save_dir / "raw",
            width=options.raw_width,
            max_files=options.raw_max_files,
            max_bytes=options.raw_max_bytes,
        )
        if options.record_all
        else None
    )
    selector = _make_selector(options)
    probe_statistics = ProbeStatistics()
    backend: WindowsGraphicsCaptureBackend | None = None
    input_recorder: InputTelemetryRecorder | None = None
    input_backend: WindowsRawInputBackend | None = None
    csv_writer = MetricsCsvWriter(options.save_dir)
    exit_code = 0
    session_anchor = ClockAnchor.sample()
    started_at = session_anchor.utc
    previous_frame: CapturedFrame | None = None
    _write_session(
        options,
        started_at,
        None,
        0,
        None,
        clock_anchor=session_anchor,
    )
    _print_banner(options)
    try:
        if options.title is None:
            print(
                f"未指定 --title：{FOREGROUND_SELECTION_DELAY_SECONDS} 秒后锁定前台窗口，"
                "请现在切回游戏。"
            )
            time.sleep(FOREGROUND_SELECTION_DELAY_SECONDS)
        backend = WindowsGraphicsCaptureBackend(
            options.title,
            capture_cursor=options.capture_cursor,
        )
        if options.record_input:
            input_recorder = InputTelemetryRecorder(
                options.save_dir,
                full_keyboard=options.input_full_keyboard,
            )
            input_backend_type = (
                FullKeyboardWindowsRawInputBackend
                if options.input_full_keyboard
                else WindowsRawInputBackend
            )
            input_backend = input_backend_type(
                backend.target.hwnd,
                input_recorder,
            )
            print("Windows Raw Input 已初始化；被动接收、未安装系统钩子")
        print(
            f"目标：{backend.target.title} | {backend.target.process_name} | "
            f"间隔 {options.interval:.2f}s"
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
                unavailable_anchor = ClockAnchor.sample()
                csv_writer.write_unavailable(
                    probe_statistics.poll_count,
                    unavailable_anchor.utc,
                    backend.target.title,
                    capture_duration * 1000,
                    monotonic_seconds=unavailable_anchor.monotonic_seconds,
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

            detection_bitmap = frame.bitmap
            if raw_archive is not None:
                _, detection_bitmap = raw_archive.save(
                    frame, probe_statistics.poll_count
                )
            metrics_started = time.perf_counter()
            decision_time = frame.metadata.monotonic_seconds
            observation = selector.observe(detection_bitmap, decision_time)
            metrics_duration_ms = (time.perf_counter() - metrics_started) * 1000
            probe_statistics.record(observation, metrics_duration_ms)
            decision = observation.decision
            saved_filename = ""
            previous_filename = ""
            if decision.should_save:
                saved_path, previous_path = archive.save_pair(
                    frame, previous_frame, probe_statistics.poll_count
                )
                local_time = frame.metadata.captured_at.astimezone().strftime("%H:%M:%S")
                retained = saved_path.name if saved_path is not None else "已因容量上限淘汰"
                saved_filename = "" if saved_path is None else saved_path.name
                previous_filename = "" if previous_path is None else previous_path.name
                print(
                    f"{local_time} | {frame.metadata.window_title} | "
                    f"reason={decision.reason} | blocks={decision.changed_block_count}/144 | "
                    f"forced={'true' if decision.forced else 'false'} | {retained}"
                )
            csv_writer.write(
                probe_statistics.poll_count,
                frame.metadata.captured_at,
                frame.metadata.window_title,
                observation,
                saved_filename,
                previous_filename,
                metrics_duration_ms,
                monotonic_seconds=frame.metadata.monotonic_seconds,
                time_source=frame.metadata.time_source,
                source_frames_drained=frame.metadata.source_frames_drained,
                capture_duration_ms=capture_duration * 1000,
            )
            previous_frame = frame
            elapsed = time.perf_counter() - iteration_started
            time.sleep(max(0.0, options.interval - elapsed))
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在干净退出……")
    except (CaptureError, InputTelemetryError) as error:
        print(f"\n探针停止：{error}", file=sys.stderr)
        exit_code = 1
    finally:
        if input_backend is not None:
            try:
                input_backend.close()
            except InputTelemetryError as error:
                exit_code = 1
                print(f"输入记录退出异常：{error}", file=sys.stderr)
        if input_recorder is not None:
            input_recorder.close()
        input_summary = (
            empty_input_summary()
            if input_recorder is None
            else input_recorder.summary()
        )
        if backend is not None:
            backend.close()
        csv_writer.close()
        summary = _build_summary(probe_statistics, archive, raw_archive)
        _print_summary(summary)
        if options.record_input:
            _print_input_summary(input_summary)
        _write_session(
            options,
            started_at,
            datetime.now(timezone.utc),
            probe_statistics.poll_count,
            summary,
            input_summary,
            clock_anchor=session_anchor,
        )
    return exit_code


def _raw_frame_timestamp(path: Path) -> datetime:
    parts = path.stem.split("-", maxsplit=2)
    if len(parts) != 3 or parts[0] != "raw":
        raise ValueError(f"无法从 raw 文件名读取时间：{path.name}")
    try:
        return datetime.strptime(parts[2], "%Y%m%dT%H%M%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise ValueError(f"raw 文件名时间格式无效：{path.name}") from error


def _raw_frame_sequence(path: Path) -> int:
    parts = path.stem.split("-", maxsplit=2)
    if len(parts) != 3 or parts[0] != "raw":
        raise ValueError(f"无法从 raw 文件名读取序号：{path.name}")
    try:
        return int(parts[1])
    except ValueError as error:
        raise ValueError(f"raw 文件名序号格式无效：{path.name}") from error


@dataclass(frozen=True, slots=True)
class ReplayFrameTime:
    wall_time: datetime
    monotonic_seconds: float
    source: Literal["presentation", "capture"]
    recorded_monotonic: bool


def _load_replay_frame_times(
    raw_dir: Path,
    paths: list[Path] | tuple[Path, ...],
) -> tuple[ReplayFrameTime, ...]:
    """Use the recorded monotonic timeline, or fall back wholly to wall time."""
    metrics_path = raw_dir.parent / "metrics.csv"
    by_sequence: dict[int, ReplayFrameTime] = {}
    if metrics_path.is_file():
        try:
            with metrics_path.open(encoding="utf-8-sig", newline="") as source:
                reader = csv.DictReader(source)
                if reader.fieldnames and "单调秒" in reader.fieldnames:
                    for row in reader:
                        if not row.get("单调秒"):
                            continue
                        sequence = int(row["序号"])
                        source_name = row.get("时间来源", "capture")
                        time_source: Literal["presentation", "capture"] = (
                            "presentation"
                            if source_name == "presentation"
                            else "capture"
                        )
                        by_sequence[sequence] = ReplayFrameTime(
                            datetime.fromisoformat(row["时间"]),
                            float(row["单调秒"]),
                            time_source,
                            True,
                        )
        except (OSError, csv.Error, KeyError, TypeError, ValueError) as error:
            raise CaptureError(f"无法读取重放时间轴 {metrics_path}：{error}") from error
    sequences = [_raw_frame_sequence(path) for path in paths]
    if by_sequence and all(sequence in by_sequence for sequence in sequences):
        return tuple(by_sequence[sequence] for sequence in sequences)
    print("旧录制缺少单调秒；本次整条时间轴回退 UTC 墙钟（仅标注一次）")
    return tuple(
        ReplayFrameTime(
            wall_time=_raw_frame_timestamp(path),
            monotonic_seconds=_raw_frame_timestamp(path).timestamp(),
            source="capture",
            recorded_monotonic=False,
        )
        for path in paths
    )


def _replay_session_monotonic_origin(
    raw_dir: Path, frame_times: Sequence[ReplayFrameTime]
) -> float:
    """Return the recorded session anchor used by relative segment manifests."""
    session_path = raw_dir.parent / "session.json"
    if (
        frame_times
        and all(item.recorded_monotonic for item in frame_times)
        and session_path.is_file()
    ):
        try:
            payload = json.loads(session_path.read_text(encoding="utf-8"))
            anchor = payload.get("时钟锚点")
            value = anchor.get("perf_counter秒") if isinstance(anchor, dict) else None
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                return float(value)
        except (OSError, json.JSONDecodeError):
            pass
    if not frame_times:
        raise CaptureError(f"无法确定空重放流的时间原点：{raw_dir}")
    return frame_times[0].monotonic_seconds


_LEGACY_GLOBAL_CHANGE_REASON = "camera" "_motion"


def _read_existing_metrics_reason_counts(raw_dir: Path) -> dict[str, int]:
    """Read an older session summary without reviving its removed classifier.

    M5-T5b deleted the global-change category. Historical metrics remain
    read-only evidence, so replay folds that old reason into
    ``persistent_change`` and reports the normalization once.
    """
    metrics_path = raw_dir.parent / "metrics.csv"
    if not metrics_path.is_file():
        return {}
    counts: dict[str, int] = {}
    normalized_count = 0
    try:
        with metrics_path.open(encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                reason = row.get("判定原因", "")
                if reason == _LEGACY_GLOBAL_CHANGE_REASON:
                    reason = "persistent_change"
                    normalized_count += 1
                if reason:
                    counts[reason] = counts.get(reason, 0) + 1
    except (OSError, csv.Error) as error:
        raise CaptureError(f"无法读取既有指标文件 {metrics_path}：{error}") from error
    if normalized_count:
        print(
            "既有 metrics.csv 含已删除的全屏变化原因；"
            f"本次读取已将 {normalized_count} 行归入 persistent_change"
        )
    return counts


def run_replay(options: ProbeOptions) -> tuple[SelectionDecision, ...]:
    """Replay one retained raw stream deterministically without writing images."""
    if options.replay_dir is None:
        raise ValueError("replay_dir is required")
    paths = sorted(options.replay_dir.glob("raw-*.jpg"))
    if not paths:
        raise CaptureError(f"重放目录中没有 raw JPEG：{options.replay_dir}")
    options.save_dir.mkdir(parents=True, exist_ok=True)
    _read_existing_metrics_reason_counts(options.replay_dir)
    frame_times = _load_replay_frame_times(options.replay_dir, paths)
    replay_scope: tuple[bool, ...] | None = None
    if options.segments_path is not None:
        from pet.core.capture_calibration import load_segments

        matching = [
            segment
            for segment in load_segments(options.segments_path)
            if (segment.session_dir / "raw").resolve()
            == options.replay_dir.resolve()
            and (
                options.segment_role is None
                or segment.role == options.segment_role
            )
        ]
        if len(matching) != 1:
            raise CaptureError(
                "--replay 配合 --segments 时必须恰好匹配一个区间；"
                f"当前匹配 {len(matching)} 个，请用 --segment-role 选择角色"
            )
        segment = matching[0]
        origin = _replay_session_monotonic_origin(options.replay_dir, frame_times)
        context = [
            (path, frame_time)
            for path, frame_time in zip(paths, frame_times, strict=True)
            if segment.end_seconds is None
            or frame_time.monotonic_seconds - origin < segment.end_seconds
        ]
        replay_scope = tuple(
            segment.contains(frame_time.monotonic_seconds - origin)
            for _, frame_time in context
        )
        if not any(replay_scope):
            raise CaptureError(f"重放区间 {segment.role!r} 内没有 raw 帧")
        paths = [item[0] for item in context]
        frame_times = tuple(item[1] for item in context)
    selector = _make_selector(options)
    probe_statistics = ProbeStatistics()
    decisions: list[SelectionDecision] = []
    first_time = frame_times[0]
    session_anchor = ClockAnchor(first_time.wall_time, first_time.monotonic_seconds)
    started_at = first_time.wall_time
    _write_session(
        options,
        started_at,
        None,
        0,
        None,
        clock_anchor=session_anchor,
    )
    print("=" * 72)
    print(f"【离线重放】读取：{options.replay_dir}")
    if options.segments_path is not None:
        print(f"只读区间清单：{options.segments_path}")
        print(f"区间角色：{segment.role}")
    print(f"输出：{options.save_dir}（只写 metrics.csv 与 session.json，不写图片）")
    print("=" * 72)
    with MetricsCsvWriter(options.save_dir) as csv_writer:
        for sequence, (path, frame_time) in enumerate(
            zip(paths, frame_times, strict=True), start=1
        ):
            captured_at = frame_time.wall_time
            with Image.open(path) as source:
                bitmap = source.convert("RGB")
            observation = selector.observe(bitmap, frame_time.monotonic_seconds)
            if replay_scope is not None and not replay_scope[sequence - 1]:
                continue
            decision = observation.decision
            decisions.append(decision)
            probe_statistics.poll_count += 1
            probe_statistics.captured_count += 1
            probe_statistics.record(observation, 0.0)
            csv_writer.write(
                sequence,
                captured_at,
                "离线重放",
                observation,
                "",
                "",
                0.0,
                monotonic_seconds=frame_time.monotonic_seconds,
                time_source=frame_time.source,
                source_frames_drained=1,
                capture_duration_ms=0.0,
            )
    summary = _build_summary(probe_statistics, None)
    _print_summary(summary)
    _write_session(
        options,
        started_at,
        datetime.now(timezone.utc),
        probe_statistics.poll_count,
        summary,
        clock_anchor=session_anchor,
    )
    return tuple(decisions)


def main() -> None:
    _configure_console_encoding()
    parser = _build_parser()
    arguments = parser.parse_args()
    if arguments.input_full_keyboard and not arguments.record_input:
        parser.error("--input-full-keyboard 必须与 --record-input 一起使用")
    if arguments.watch and arguments.segments is not None:
        parser.error("--segments 只适用于 --replay 或 --calibrate")
    if arguments.segment_role is not None and arguments.replay is None:
        parser.error("--segment-role 只适用于 --replay")
    if arguments.replay is not None and arguments.record_input:
        parser.error("--record-input 只适用于 --watch；离线重放不读取实时键鼠")
    if arguments.calibrate is not None:
        from pet.core.capture_calibration import CalibrationOptions, run_calibration

        output_dir = arguments.save_dir or _default_calibration_dir()
        try:
            run_calibration(
                CalibrationOptions(
                    session_specs=tuple(arguments.calibrate),
                    output_dir=output_dir,
                    segments_path=arguments.segments,
                    grid_path=arguments.grid,
                    sample_stride=arguments.sample_stride,
                    strong_block_delta=arguments.strong_block_delta,
                    input_motion_threshold=arguments.input_motion_threshold,
                )
            )
            raise SystemExit(0)
        except CaptureError as error:
            print(f"操作停止：{error}", file=sys.stderr)
            raise SystemExit(1) from error
    options = ProbeOptions(
        interval=arguments.interval,
        title=arguments.title,
        save_dir=arguments.save_dir or _default_save_dir(),
        min_save_interval=arguments.min_save_interval,
        max_silence=arguments.max_silence,
        noise_window=arguments.noise_window,
        noise_multiplier=arguments.noise_multiplier,
        noise_margin=arguments.noise_margin,
        persistence_polls=arguments.persistence_polls,
        region_sparsity_max=arguments.region_sparsity_max,
        label=arguments.label,
        capture_cursor=arguments.capture_cursor,
        record_all=arguments.record_all,
        record_input=arguments.record_input,
        input_full_keyboard=arguments.input_full_keyboard,
        raw_width=arguments.raw_width,
        raw_max_files=arguments.raw_max_files,
        raw_max_bytes=arguments.raw_max_bytes,
        replay_dir=arguments.replay,
        segments_path=arguments.segments,
        segment_role=arguments.segment_role,
    )
    try:
        if arguments.replay is not None:
            run_replay(options)
            raise SystemExit(0)
        raise SystemExit(run_probe(options))
    except CaptureError as error:
        print(f"操作停止：{error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
