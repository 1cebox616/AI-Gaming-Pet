"""Single-window capture and a local frame-change probe.

The production-facing classes in this module never select a monitor or the
desktop.  A Windows HWND is resolved first, then passed to Windows Graphics
Capture through the optional ``zbl`` binding.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from types import ModuleType
from typing import Protocol, Self

import numpy as np
import numpy.typing as npt
from PIL import Image

DEFAULT_INTERVAL_SECONDS = 2.0
DEFAULT_THRESHOLD = 0.02
DEFAULT_CHANGE_WIDTH = 96
DEFAULT_MAX_FILES = 500
DEFAULT_MAX_BYTES = 200 * 1024 * 1024
FOREGROUND_SELECTION_DELAY_SECONDS = 3
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


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

    def grab(self) -> npt.NDArray[np.uint8]: ...


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

    def capture_frame(self) -> CapturedFrame:
        """Copy the next WGC frame so its bitmap outlives the native buffer."""
        if self._session is None:
            raise CaptureError("截屏会话已经关闭；请重新启动探针")
        try:
            raw = np.asarray(self._session.grab(), dtype=np.uint8).copy()
        except StopIteration as error:
            raise CaptureError("目标窗口已关闭，截屏会话随之结束") from error
        except Exception as error:
            raise CaptureError(f"抓取目标窗口失败：{error}") from error
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


@dataclass(frozen=True, slots=True)
class FrameChangeDetector:
    """Stateless, deterministic comparison of two injectable bitmaps."""

    threshold: float = DEFAULT_THRESHOLD
    target_width: int = DEFAULT_CHANGE_WIDTH

    def __post_init__(self) -> None:
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if self.target_width <= 0:
            raise ValueError("target_width must be positive")

    def difference(self, previous: FrameLike, current: FrameLike) -> float:
        """Return normalized mean grayscale pixel difference in the range 0..1."""
        previous_gray = _to_grayscale(previous)
        current_gray = _to_grayscale(current)
        aspect_ratio = min(
            previous_gray.height / previous_gray.width,
            current_gray.height / current_gray.width,
        )
        target_height = max(1, round(self.target_width * aspect_ratio))
        size = (self.target_width, target_height)
        previous_small = np.asarray(
            previous_gray.resize(size, Image.Resampling.BILINEAR),
            dtype=np.float32,
        )
        current_small = np.asarray(
            current_gray.resize(size, Image.Resampling.BILINEAR),
            dtype=np.float32,
        )
        return float(np.mean(np.abs(previous_small - current_small)) / 255.0)

    def has_changed(self, previous: FrameLike, current: FrameLike) -> bool:
        """Return true only when the pure difference score exceeds the threshold."""
        return self.difference(previous, current) > self.threshold


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

    captured_count: int = 0
    saved_count: int = 0
    differences: list[float] = field(default_factory=list)
    capture_durations: list[float] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ProbeOptions:
    """Validated command-line options for the foreground watch loop."""

    interval: float
    threshold: float
    title: str | None
    save_dir: Path


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
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
    print("仅首帧和超过阈值的变化帧落盘；Ctrl+C 停止")
    print("=" * 72)


def _print_summary(statistics_: ProbeStatistics, archive: CaptureArchive) -> None:
    print("\n截屏探针汇总")
    print(f"抓帧数：{statistics_.captured_count}")
    print(
        f"落盘数：{statistics_.saved_count}（当前保留 {archive.retained_count} 张，"
        f"{archive.retained_bytes / (1024 * 1024):.2f} MB）"
    )
    if statistics_.differences:
        ordered = sorted(statistics_.differences)
        print(
            "差异值分布："
            f"最小 {ordered[0]:.5f} / P50 {_percentile(ordered, 0.50):.5f} / "
            f"P90 {_percentile(ordered, 0.90):.5f} / 最大 {ordered[-1]:.5f}"
        )
    else:
        print("差异值分布：没有足够帧可比较")
    if statistics_.capture_durations:
        print(
            "单次抓帧耗时："
            f"中位 {statistics.median(statistics_.capture_durations) * 1000:.1f} ms / "
            f"最大 {max(statistics_.capture_durations) * 1000:.1f} ms"
        )
    else:
        print("单次抓帧耗时：没有成功抓帧")


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
    probe_statistics = ProbeStatistics()
    backend: WindowsGraphicsCaptureBackend | None = None
    exit_code = 0
    previous: Image.Image | None = None
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
        while True:
            iteration_started = time.perf_counter()
            capture_started = time.perf_counter()
            frame = backend.capture_frame()
            probe_statistics.capture_durations.append(time.perf_counter() - capture_started)
            probe_statistics.captured_count += 1

            difference = None if previous is None else detector.difference(previous, frame.bitmap)
            if difference is not None:
                probe_statistics.differences.append(difference)
            changed = difference is None or difference > options.threshold
            if changed:
                saved_path = archive.save(frame, probe_statistics.captured_count)
                probe_statistics.saved_count += 1
                local_time = frame.metadata.captured_at.astimezone().strftime("%H:%M:%S")
                difference_text = "首帧" if difference is None else f"{difference:.5f}"
                retained = saved_path.name if saved_path is not None else "已因容量上限淘汰"
                print(
                    f"{local_time} | {frame.metadata.window_title} | "
                    f"差异={difference_text} | {retained}"
                )
            previous = frame.bitmap
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
        _print_summary(probe_statistics, archive)
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
    )
    raise SystemExit(run_probe(options))


if __name__ == "__main__":
    main()
