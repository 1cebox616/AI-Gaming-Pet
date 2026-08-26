"""Passive, allowlisted Windows Raw Input telemetry for capture sessions.

The Windows backend uses a message-only window and Raw Input.  It deliberately
does not install system hooks because hooks are a high-risk anti-cheat signal.
This project also permanently forbids input-simulation or injection APIs: this
module only receives device events and never writes input back to the system.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import csv
import ctypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import statistics
import sys
import threading
import time
from typing import Literal, Protocol, Self, cast


INPUT_WHITELIST_VERSION = "v1"
MOUSE_SUMMARY_WINDOW_SECONDS = 0.100
ACTION_INPUT_RETENTION_SECONDS = 65.0
ACTION_INPUT_EMPTY_TEXT = "此窗口内无玩家输入"
# The two M5 calibration recordings have per-100 ms movement medians of 43.6
# and 106.9 raw units.  Five hundred units separates their approximate
# one-second light/large regimes; this presentation threshold does not affect
# capture or frame-selection decisions.
MOUSE_LARGE_MOVEMENT_UNITS = 500.0

KEYBOARD_INPUT_NAMES = (
    "W",
    "A",
    "S",
    "D",
    "Shift",
    "Ctrl",
    "Alt",
    "Space",
    "Tab",
    "E",
    "Q",
    "R",
    "F",
    "C",
    "V",
    "G",
    "Esc",
    "F1",
    "F2",
    "F3",
    "F4",
    "F5",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
)
MOUSE_INPUT_NAMES = (
    "MouseLeft",
    "MouseRight",
    "MouseMiddle",
    "Mouse4",
    "Mouse5",
    "WheelUp",
    "WheelDown",
)
INPUT_NAME_WHITELIST = frozenset((*KEYBOARD_INPUT_NAMES, *MOUSE_INPUT_NAMES))

INPUT_CSV_HEADER = ("时间", "单调秒", "事件类型", "键名", "dx", "dy")
InputEventType = Literal["按下", "抬起", "滚轮", "移动汇总", "焦点丢失"]
InputSampleKind = Literal["key", "mouse_button", "wheel", "move"]


class InputTelemetryError(RuntimeError):
    """An input-recorder failure phrased for the probe operator."""


@dataclass(frozen=True, slots=True)
class ClockAnchor:
    """A near-simultaneous wall-clock/perf-counter pair for one session."""

    utc: datetime
    monotonic_seconds: float

    @classmethod
    def sample(cls) -> ClockAnchor:
        before = time.perf_counter()
        utc = datetime.now(timezone.utc)
        after = time.perf_counter()
        return cls(utc, (before + after) / 2.0)

    def wall_from_monotonic(self, value: float) -> datetime:
        return self.utc + timedelta(seconds=value - self.monotonic_seconds)

    def monotonic_from_wall(self, value: datetime) -> float:
        return self.monotonic_seconds + (_utc(value) - self.utc).total_seconds()


@dataclass(frozen=True, slots=True)
class InputSample:
    """One injectable device sample before privacy filtering and aggregation."""

    timestamp: datetime
    kind: InputSampleKind
    foreground: bool
    name: str = ""
    pressed: bool | None = None
    dx: int = 0
    dy: int = 0
    relative: bool = True
    monotonic_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class InputRecord:
    """One allowlisted row eligible to be written to input.csv."""

    timestamp: datetime
    event_type: InputEventType
    name: str = ""
    dx: int = 0
    dy: int = 0
    monotonic_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class ActionInputEvent:
    """One action-only event eligible for the model context timeline."""

    monotonic_seconds: float
    event_type: InputEventType
    name: str = ""
    dx: int = 0
    dy: int = 0
    absolute_dx: int = 0
    absolute_dy: int = 0
    direction_known: bool = True


@dataclass(frozen=True, slots=True)
class LoadedActionInput:
    """Privacy-filtered replay timeline plus source limitations."""

    timeline: ActionInputTimeline
    csv_missing: bool
    direction_available: bool


class RawInputProcessorLike(Protocol):
    def set_foreground(
        self,
        foreground: bool,
        timestamp: datetime,
        monotonic_seconds: float | None = None,
    ) -> None: ...

    def process(self, sample: InputSample) -> None: ...

    def flush_due(
        self,
        timestamp: datetime,
        monotonic_seconds: float | None = None,
    ) -> None: ...


class RawInputOwnerLike(Protocol):
    processor: RawInputProcessorLike


@dataclass(slots=True)
class InputStatistics:
    """Counters derived only from rows that survived the privacy filter."""

    event_count: int = 0
    press_counts: dict[str, int] = field(default_factory=dict)
    movement_magnitudes: list[float] = field(default_factory=list)
    focus_lost_count: int = 0

    def record(self, event: InputRecord) -> None:
        self.event_count += 1
        if event.event_type == "按下":
            self.press_counts[event.name] = self.press_counts.get(event.name, 0) + 1
        elif event.event_type == "移动汇总":
            self.movement_magnitudes.append(math.hypot(event.dx, event.dy))
        elif event.event_type == "焦点丢失":
            self.focus_lost_count += 1

    def summary(self) -> dict[str, object]:
        """Return session fields; displacement is per 100 ms aggregate row."""
        ordered = sorted(self.movement_magnitudes)
        return {
            "白名单版本": INPUT_WHITELIST_VERSION,
            "事件总数": self.event_count,
            "按键名分组的按下次数": dict(sorted(self.press_counts.items())),
            "鼠标位移量口径": "每个100毫秒移动汇总行的 hypot(sum(abs(dx)), sum(abs(dy)))",
            "鼠标位移量中位": None if not ordered else statistics.median(ordered),
            "鼠标位移量P90": None if not ordered else _percentile(ordered, 0.90),
            "focus_lost次数": self.focus_lost_count,
        }

    def report_safe_press_counts(self) -> dict[str, int]:
        """Aggregate non-action keys before any statistics enter tracked reports.

        ``session.json`` is local and deliberately retains per-key development
        data.  A committed report must use this projection so typing content is
        represented only as one ``其他键合计`` count.
        """
        safe = {
            name: count
            for name, count in self.press_counts.items()
            if name in INPUT_NAME_WHITELIST
        }
        other_count = sum(
            count
            for name, count in self.press_counts.items()
            if name not in INPUT_NAME_WHITELIST
        )
        if other_count:
            safe["其他键合计"] = other_count
        return dict(sorted(safe.items()))


class InputCsvWriter:
    """Write and immediately flush each privacy-filtered input row."""

    def __init__(self, save_dir: Path) -> None:
        save_dir.mkdir(parents=True, exist_ok=True)
        self.path = save_dir / "input.csv"
        self._file = self.path.open("w", encoding="utf-8-sig", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(INPUT_CSV_HEADER)
        self._file.flush()

    def write(self, event: InputRecord) -> None:
        self._writer.writerow(
            (
                event.timestamp.astimezone(timezone.utc).isoformat(),
                f"{event.monotonic_seconds:.9f}",
                event.event_type,
                event.name,
                event.dx if event.event_type == "移动汇总" else "",
                event.dy if event.event_type == "移动汇总" else "",
            )
        )
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class InputEventProcessor:
    """Pure allowlist, focus gate, and fixed-window movement aggregator."""

    def __init__(
        self,
        sink: Callable[[InputRecord], None],
        *,
        movement_window_seconds: float = MOUSE_SUMMARY_WINDOW_SECONDS,
        full_keyboard: bool = False,
    ) -> None:
        if movement_window_seconds <= 0:
            raise ValueError("movement window must be positive")
        self._sink = sink
        self._window_seconds = movement_window_seconds
        self._full_keyboard = full_keyboard
        self._focused = False
        self._movement_started_at_monotonic: float | None = None
        self._movement_dx = 0
        self._movement_dy = 0
        self.statistics = InputStatistics()

    @property
    def full_keyboard(self) -> bool:
        return self._full_keyboard

    def set_foreground(
        self,
        foreground: bool,
        timestamp: datetime,
        monotonic_seconds: float | None = None,
    ) -> None:
        """Update the exact target-window focus state and emit one loss edge."""
        normalized = _utc(timestamp)
        monotonic = _event_monotonic(normalized, monotonic_seconds)
        if foreground:
            self._focused = True
            return
        if self._focused:
            self._flush_movement(normalized, monotonic, force=True)
            self._emit(
                InputRecord(
                    normalized,
                    "焦点丢失",
                    monotonic_seconds=monotonic,
                )
            )
        self._focused = False

    def process(self, sample: InputSample) -> None:
        """Consume one synthetic or Raw Input sample without retaining rejected data."""
        timestamp = _utc(sample.timestamp)
        monotonic = _event_monotonic(timestamp, sample.monotonic_seconds)
        self.set_foreground(sample.foreground, timestamp, monotonic)
        if not self._focused:
            return
        self.flush_due(timestamp, monotonic)
        if sample.kind in {"key", "mouse_button"}:
            allowed = sample.name in INPUT_NAME_WHITELIST or (
                self._full_keyboard and sample.kind == "key" and bool(sample.name)
            )
            if not allowed or sample.pressed is None:
                return
            event_type: InputEventType = "按下" if sample.pressed else "抬起"
            self._emit(
                InputRecord(
                    timestamp,
                    event_type,
                    sample.name,
                    monotonic_seconds=monotonic,
                )
            )
            return
        if sample.kind == "wheel":
            if sample.name not in {"WheelUp", "WheelDown"}:
                return
            self._emit(
                InputRecord(
                    timestamp,
                    "滚轮",
                    sample.name,
                    monotonic_seconds=monotonic,
                )
            )
            return
        if sample.kind == "move" and sample.relative:
            self._accumulate_movement(monotonic, sample.dx, sample.dy)

    def flush_due(
        self,
        timestamp: datetime,
        monotonic_seconds: float | None = None,
    ) -> None:
        """Flush one complete movement window; timers call this when input is idle."""
        normalized = _utc(timestamp)
        monotonic = _event_monotonic(normalized, monotonic_seconds)
        started = self._movement_started_at_monotonic
        if started is None:
            return
        if monotonic - started + 1e-6 >= self._window_seconds:
            self._flush_movement(normalized, monotonic, force=True)

    def finish(
        self,
        timestamp: datetime,
        monotonic_seconds: float | None = None,
    ) -> None:
        normalized = _utc(timestamp)
        self._flush_movement(
            normalized,
            _event_monotonic(normalized, monotonic_seconds),
            force=True,
        )

    def _accumulate_movement(self, monotonic_seconds: float, dx: int, dy: int) -> None:
        if dx == 0 and dy == 0:
            return
        if self._movement_started_at_monotonic is None:
            self._movement_started_at_monotonic = monotonic_seconds
        self._movement_dx += abs(dx)
        self._movement_dy += abs(dy)

    def _flush_movement(
        self,
        timestamp: datetime,
        monotonic_seconds: float,
        *,
        force: bool,
    ) -> None:
        if self._movement_started_at_monotonic is None:
            return
        if force and (self._movement_dx or self._movement_dy):
            self._emit(
                InputRecord(
                    timestamp,
                    "移动汇总",
                    dx=self._movement_dx,
                    dy=self._movement_dy,
                    monotonic_seconds=monotonic_seconds,
                )
            )
        self._movement_started_at_monotonic = None
        self._movement_dx = 0
        self._movement_dy = 0

    def _emit(self, event: InputRecord) -> None:
        self._sink(event)
        self.statistics.record(event)


class InputTelemetryRecorder:
    """Own input.csv and the injectable processor for one capture session."""

    def __init__(self, save_dir: Path, *, full_keyboard: bool = False) -> None:
        self.writer = InputCsvWriter(save_dir)
        self.processor = InputEventProcessor(
            self.writer.write,
            full_keyboard=full_keyboard,
        )
        self._closed = False

    def close(self, ended_at: datetime | None = None) -> None:
        if self._closed:
            return
        anchor = ClockAnchor.sample()
        ended = ended_at or anchor.utc
        self.processor.finish(
            ended,
            anchor.monotonic_from_wall(ended),
        )
        self.writer.close()
        self._closed = True

    def summary(self) -> dict[str, object]:
        return self.processor.statistics.summary()


class ActionInputTimeline:
    """Thread-safe action-only ring buffer with deterministic window summaries."""

    def __init__(self, retention_seconds: float | None = ACTION_INPUT_RETENTION_SECONDS) -> None:
        if retention_seconds is not None and retention_seconds <= 0:
            raise ValueError("action input retention must be positive")
        self._retention_seconds = retention_seconds
        self._events: deque[ActionInputEvent] = deque()
        self._lock = threading.Lock()

    def append(self, event: ActionInputEvent) -> None:
        """Retain only action names; rejected identities never enter memory."""
        if event.name and event.name not in INPUT_NAME_WHITELIST:
            return
        with self._lock:
            self._events.append(event)
            if self._retention_seconds is not None:
                cutoff = event.monotonic_seconds - self._retention_seconds
                while self._events and self._events[0].monotonic_seconds < cutoff:
                    self._events.popleft()

    def summarize_window(
        self,
        start_exclusive: float | None,
        end_inclusive: float,
    ) -> str:
        """Summarize exactly ``(start, end]`` without exposing raw key rows."""
        if not math.isfinite(end_inclusive):
            raise ValueError("action input window end must be finite")
        if start_exclusive is not None:
            if not math.isfinite(start_exclusive):
                raise ValueError("action input window start must be finite")
            if end_inclusive < start_exclusive:
                raise ValueError("action input window end precedes start")
        with self._lock:
            if self._retention_seconds is not None:
                cutoff = end_inclusive - self._retention_seconds
                while self._events and self._events[0].monotonic_seconds < cutoff:
                    self._events.popleft()
            events = tuple(sorted(self._events, key=lambda item: item.monotonic_seconds))
        return _summarize_action_events(events, start_exclusive, end_inclusive)

    def close(self) -> None:
        """Replay timelines own no OS or file resource."""


class ActionInputEventProcessor:
    """Action-only focus gate preserving signed mouse direction in memory."""

    def __init__(
        self,
        sink: Callable[[ActionInputEvent], None],
        *,
        movement_window_seconds: float = MOUSE_SUMMARY_WINDOW_SECONDS,
    ) -> None:
        if movement_window_seconds <= 0:
            raise ValueError("movement window must be positive")
        self._sink = sink
        self._window_seconds = movement_window_seconds
        self._focused = False
        self._movement_started_at_monotonic: float | None = None
        self._movement_dx = 0
        self._movement_dy = 0
        self._movement_absolute_dx = 0
        self._movement_absolute_dy = 0

    def set_foreground(
        self,
        foreground: bool,
        timestamp: datetime,
        monotonic_seconds: float | None = None,
    ) -> None:
        normalized = _utc(timestamp)
        monotonic = _event_monotonic(normalized, monotonic_seconds)
        if foreground:
            self._focused = True
            return
        if self._focused:
            self._flush_movement(monotonic)
            self._sink(ActionInputEvent(monotonic, "焦点丢失"))
        self._focused = False

    def process(self, sample: InputSample) -> None:
        timestamp = _utc(sample.timestamp)
        monotonic = _event_monotonic(timestamp, sample.monotonic_seconds)
        self.set_foreground(sample.foreground, timestamp, monotonic)
        if not self._focused:
            return
        self.flush_due(timestamp, monotonic)
        if sample.kind in {"key", "mouse_button"}:
            if sample.name not in INPUT_NAME_WHITELIST or sample.pressed is None:
                return
            event_type: InputEventType = "按下" if sample.pressed else "抬起"
            self._sink(ActionInputEvent(monotonic, event_type, sample.name))
            return
        if sample.kind == "wheel":
            if sample.name not in {"WheelUp", "WheelDown"}:
                return
            self._sink(ActionInputEvent(monotonic, "滚轮", sample.name))
            return
        if sample.kind == "move" and sample.relative:
            self._accumulate_movement(monotonic, sample.dx, sample.dy)

    def flush_due(
        self,
        timestamp: datetime,
        monotonic_seconds: float | None = None,
    ) -> None:
        normalized = _utc(timestamp)
        monotonic = _event_monotonic(normalized, monotonic_seconds)
        started = self._movement_started_at_monotonic
        if started is not None and monotonic - started + 1e-6 >= self._window_seconds:
            self._flush_movement(monotonic)

    def finish(
        self,
        timestamp: datetime,
        monotonic_seconds: float | None = None,
    ) -> None:
        normalized = _utc(timestamp)
        self._flush_movement(_event_monotonic(normalized, monotonic_seconds))

    def _accumulate_movement(self, monotonic_seconds: float, dx: int, dy: int) -> None:
        if dx == 0 and dy == 0:
            return
        if self._movement_started_at_monotonic is None:
            self._movement_started_at_monotonic = monotonic_seconds
        self._movement_dx += dx
        self._movement_dy += dy
        self._movement_absolute_dx += abs(dx)
        self._movement_absolute_dy += abs(dy)

    def _flush_movement(self, monotonic_seconds: float) -> None:
        if self._movement_started_at_monotonic is None:
            return
        if self._movement_absolute_dx or self._movement_absolute_dy:
            self._sink(
                ActionInputEvent(
                    monotonic_seconds,
                    "移动汇总",
                    dx=self._movement_dx,
                    dy=self._movement_dy,
                    absolute_dx=self._movement_absolute_dx,
                    absolute_dy=self._movement_absolute_dy,
                    direction_known=True,
                )
            )
        self._movement_started_at_monotonic = None
        self._movement_dx = 0
        self._movement_dy = 0
        self._movement_absolute_dx = 0
        self._movement_absolute_dy = 0


_MOUSE_LABELS = {
    "MouseLeft": "左键",
    "MouseRight": "右键",
    "MouseMiddle": "中键",
    "Mouse4": "鼠标侧键4",
    "Mouse5": "鼠标侧键5",
}


def _summarize_action_events(
    events: tuple[ActionInputEvent, ...],
    start_exclusive: float | None,
    end_inclusive: float,
) -> str:
    start = float("-inf") if start_exclusive is None else start_exclusive
    active: dict[str, float] = {}
    press_times: dict[str, list[float]] = {}
    held_seconds: dict[str, float] = {}
    wheel_counts: dict[str, int] = {}
    movement: list[ActionInputEvent] = []

    def finish_active(name: str, at: float) -> None:
        pressed_at = active.pop(name)
        overlap_start = max(pressed_at, start)
        if at > overlap_start:
            held_seconds[name] = held_seconds.get(name, 0.0) + (at - overlap_start)

    for event in events:
        at = event.monotonic_seconds
        if at > end_inclusive:
            break
        in_window = at > start
        if event.event_type == "按下" and event.name:
            if event.name not in active:
                active[event.name] = at
                if in_window:
                    press_times.setdefault(event.name, []).append(at)
        elif event.event_type == "抬起" and event.name:
            if event.name in active:
                finish_active(event.name, at)
        elif event.event_type == "滚轮" and event.name and in_window:
            wheel_counts[event.name] = wheel_counts.get(event.name, 0) + 1
        elif event.event_type == "移动汇总" and in_window:
            movement.append(event)
        elif event.event_type == "焦点丢失":
            for name in tuple(active):
                finish_active(name, at)
    for name in tuple(active):
        finish_active(name, end_inclusive)

    parts: list[str] = []
    for name in KEYBOARD_INPUT_NAMES:
        presses = press_times.get(name, [])
        duration = held_seconds.get(name, 0.0)
        if len(presses) >= 2:
            parts.append(_click_summary(name, presses))
        elif presses or duration > 0:
            parts.append(f"{name} 按住 {duration:.1f} 秒")
    for name in MOUSE_INPUT_NAMES:
        if name in {"WheelUp", "WheelDown"}:
            continue
        presses = press_times.get(name, [])
        duration = held_seconds.get(name, 0.0)
        label = _MOUSE_LABELS[name]
        if presses:
            parts.append(_click_summary(label, presses))
        elif duration > 0:
            parts.append(f"{label}按住 {duration:.1f} 秒")
    if wheel_counts.get("WheelUp"):
        parts.append(f"滚轮向上 {wheel_counts['WheelUp']} 次")
    if wheel_counts.get("WheelDown"):
        parts.append(f"滚轮向下 {wheel_counts['WheelDown']} 次")
    if movement:
        parts.append(_movement_summary(movement))
    return "；".join(parts) if parts else ACTION_INPUT_EMPTY_TEXT


def _click_summary(label: str, press_times: list[float]) -> str:
    count = len(press_times)
    value = f"{label}点击 {count} 次"
    if count >= 2:
        intervals = [right - left for left, right in zip(press_times, press_times[1:])]
        value += f"（平均间隔约 {statistics.mean(intervals):.2f} 秒）"
    return value


def _movement_summary(events: list[ActionInputEvent]) -> str:
    absolute_x = sum(event.absolute_dx for event in events)
    absolute_y = sum(event.absolute_dy for event in events)
    magnitude = sum(math.hypot(event.absolute_dx, event.absolute_dy) for event in events)
    grade = "大幅" if magnitude >= MOUSE_LARGE_MOVEMENT_UNITS else "轻微"
    if all(event.direction_known for event in events):
        net_x = sum(event.dx for event in events)
        net_y = sum(event.dy for event in events)
        if abs(net_x) >= abs(net_y) and net_x:
            direction = "向右" if net_x > 0 else "向左"
        elif net_y:
            direction = "向下" if net_y > 0 else "向上"
        else:
            direction = "以横向为主" if absolute_x >= absolute_y else "以纵向为主"
    else:
        direction = "以横向为主" if absolute_x >= absolute_y else "以纵向为主"
    return f"鼠标{direction}{grade}移动"


# Raw Input constants and structures. No hook or input-writing API is declared.
_WM_INPUT = 0x00FF
_WM_TIMER = 0x0113
_WM_CLOSE = 0x0010
_WM_DESTROY = 0x0002
_RID_INPUT = 0x10000003
_RIM_TYPEMOUSE = 0
_RIM_TYPEKEYBOARD = 1
_RIDEV_INPUTSINK = 0x00000100
_RIDEV_REMOVE = 0x00000001
_HID_USAGE_PAGE_GENERIC = 0x01
_HID_USAGE_GENERIC_MOUSE = 0x02
_HID_USAGE_GENERIC_KEYBOARD = 0x06
_RI_KEY_BREAK = 0x0001
_RI_KEY_E0 = 0x0002
_MOUSE_MOVE_ABSOLUTE = 0x0001
_RI_MOUSE_LEFT_BUTTON_DOWN = 0x0001
_RI_MOUSE_LEFT_BUTTON_UP = 0x0002
_RI_MOUSE_RIGHT_BUTTON_DOWN = 0x0004
_RI_MOUSE_RIGHT_BUTTON_UP = 0x0008
_RI_MOUSE_MIDDLE_BUTTON_DOWN = 0x0010
_RI_MOUSE_MIDDLE_BUTTON_UP = 0x0020
_RI_MOUSE_BUTTON_4_DOWN = 0x0040
_RI_MOUSE_BUTTON_4_UP = 0x0080
_RI_MOUSE_BUTTON_5_DOWN = 0x0100
_RI_MOUSE_BUTTON_5_UP = 0x0200
_RI_MOUSE_WHEEL = 0x0400


class _RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = (
        ("usUsagePage", ctypes.c_uint16),
        ("usUsage", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("hwndTarget", ctypes.c_void_p),
    )


class _RAWINPUTHEADER(ctypes.Structure):
    _fields_ = (
        ("dwType", ctypes.c_uint32),
        ("dwSize", ctypes.c_uint32),
        ("hDevice", ctypes.c_void_p),
        ("wParam", ctypes.c_size_t),
    )


class _RAWKEYBOARD(ctypes.Structure):
    _fields_ = (
        ("MakeCode", ctypes.c_uint16),
        ("Flags", ctypes.c_uint16),
        ("Reserved", ctypes.c_uint16),
        ("VKey", ctypes.c_uint16),
        ("Message", ctypes.c_uint32),
        ("ExtraInformation", ctypes.c_uint32),
    )


class _BUTTON_FIELDS(ctypes.Structure):
    _fields_ = (
        ("usButtonFlags", ctypes.c_uint16),
        ("usButtonData", ctypes.c_uint16),
    )


class _MOUSE_BUTTONS(ctypes.Union):
    _anonymous_ = ("fields",)
    _fields_ = (
        ("ulButtons", ctypes.c_uint32),
        ("fields", _BUTTON_FIELDS),
    )


class _RAWMOUSE(ctypes.Structure):
    _anonymous_ = ("buttons",)
    _fields_ = (
        ("usFlags", ctypes.c_uint16),
        ("buttons", _MOUSE_BUTTONS),
        ("ulRawButtons", ctypes.c_uint32),
        ("lLastX", ctypes.c_int32),
        ("lLastY", ctypes.c_int32),
        ("ulExtraInformation", ctypes.c_uint32),
    )


class _RAW_PAYLOAD(ctypes.Union):
    _fields_ = (("mouse", _RAWMOUSE), ("keyboard", _RAWKEYBOARD))


class _RAWINPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = (("header", _RAWINPUTHEADER), ("data", _RAW_PAYLOAD))


class _WNDCLASSW(ctypes.Structure):
    _fields_ = (
        ("style", ctypes.c_uint32),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int32),
        ("cbWndExtra", ctypes.c_int32),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    )


class _MSG(ctypes.Structure):
    _fields_ = (
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint32),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32),
        ("pt_x", ctypes.c_int32),
        ("pt_y", ctypes.c_int32),
        ("lPrivate", ctypes.c_uint32),
    )


_VIRTUAL_KEY_NAMES = {
    0x09: "Tab",
    0x10: "Shift",
    0x11: "Ctrl",
    0x12: "Alt",
    0x1B: "Esc",
    0x20: "Space",
    0x41: "A",
    0x43: "C",
    0x44: "D",
    0x45: "E",
    0x46: "F",
    0x47: "G",
    0x51: "Q",
    0x52: "R",
    0x53: "S",
    0x56: "V",
    0x57: "W",
    0x70: "F1",
    0x71: "F2",
    0x72: "F3",
    0x73: "F4",
    0x74: "F5",
    0xA0: "Shift",
    0xA1: "Shift",
    0xA2: "Ctrl",
    0xA3: "Ctrl",
    0xA4: "Alt",
    0xA5: "Alt",
    **{code: chr(code) for code in range(0x31, 0x3A)},
}


def _full_keyboard_name(user32: object, keyboard: _RAWKEYBOARD) -> str | None:
    """Resolve one Raw Input key without installing a keyboard hook."""
    virtual_key = int(keyboard.VKey)
    if virtual_key in {0, 0xFF}:
        return None
    if 0x30 <= virtual_key <= 0x39 or 0x41 <= virtual_key <= 0x5A:
        return chr(virtual_key)
    scan_code = int(keyboard.MakeCode)
    l_param = scan_code << 16
    if keyboard.Flags & _RI_KEY_E0:
        l_param |= 1 << 24
    buffer = ctypes.create_unicode_buffer(128)
    copied = int(user32.GetKeyNameTextW(l_param, buffer, len(buffer)))
    if copied > 0 and buffer.value:
        return buffer.value
    return f"VK_0x{virtual_key:02X}"


class WindowsRawInputBackend:
    """Receive allowlisted Raw Input in a message-only window."""

    def __init__(
        self,
        target_hwnd: int,
        recorder: RawInputOwnerLike,
        *,
        ready_timeout_seconds: float = 5.0,
    ) -> None:
        if sys.platform != "win32":
            raise InputTelemetryError(
                "键鼠输入记录只支持 Windows；当前平台无法初始化 Windows Raw Input"
            )
        if not target_hwnd:
            raise InputTelemetryError("目标游戏窗口句柄无效，无法启动输入记录")
        self._target_hwnd = target_hwnd
        self._recorder = recorder
        self._ready = threading.Event()
        self._thread_error: Exception | None = None
        self._window_handle = 0
        self._user32: object | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="pet-raw-input",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(ready_timeout_seconds):
            raise InputTelemetryError(
                "Windows Raw Input 初始化超时；未安装钩子，也未降级为静默失败"
            )
        if self._thread_error is not None:
            raise InputTelemetryError(
                f"Windows Raw Input 初始化失败：{self._thread_error}"
            ) from self._thread_error

    def close(self) -> None:
        user32 = self._user32
        if user32 is not None and self._window_handle:
            user32.PostMessageW(self._window_handle, _WM_CLOSE, 0, 0)
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            raise InputTelemetryError("Windows Raw Input 消息线程未能在 5 秒内退出")
        if self._thread_error is not None:
            raise InputTelemetryError(
                f"Windows Raw Input 运行失败：{self._thread_error}"
            ) from self._thread_error

    def _run(self) -> None:
        try:
            self._message_loop()
        except Exception as error:
            self._thread_error = error
            self._ready.set()

    def _message_loop(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_functions(user32, kernel32)
        self._user32 = user32
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )

        @callback_type
        def window_proc(
            hwnd: int | None,
            message: int,
            w_param: int,
            l_param: int,
        ) -> int:
            try:
                if message == _WM_INPUT:
                    self._receive_raw_input(user32, l_param)
                    return 0
                if message == _WM_TIMER:
                    now = datetime.now(timezone.utc)
                    monotonic = time.perf_counter()
                    self._update_focus(user32, now, monotonic)
                    self._recorder.processor.flush_due(now, monotonic)
                    return 0
                if message == _WM_CLOSE:
                    user32.DestroyWindow(hwnd)
                    return 0
                if message == _WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
                return int(user32.DefWindowProcW(hwnd, message, w_param, l_param))
            except Exception as error:
                self._thread_error = error
                user32.PostQuitMessage(1)
                return 0

        class_name = f"PetRawInputWindow_{threading.get_ident()}"
        instance = kernel32.GetModuleHandleW(None)
        window_class = _WNDCLASSW(
            lpfnWndProc=ctypes.cast(window_proc, ctypes.c_void_p),
            hInstance=instance,
            lpszClassName=class_name,
        )
        atom = user32.RegisterClassW(ctypes.byref(window_class))
        if not atom:
            raise _last_windows_error("注册 Raw Input 消息窗口")
        created_hwnd = 0
        devices_registered = False
        try:
            hwnd_message = ctypes.c_void_p(-3 & ((1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1))
            hwnd = user32.CreateWindowExW(
                0,
                class_name,
                class_name,
                0,
                0,
                0,
                0,
                0,
                hwnd_message,
                None,
                instance,
                None,
            )
            if not hwnd:
                raise _last_windows_error("创建 Raw Input 消息窗口")
            created_hwnd = int(hwnd)
            self._window_handle = created_hwnd
            self._register_devices(user32, self._window_handle)
            devices_registered = True
            if not user32.SetTimer(self._window_handle, 1, 10, None):
                raise _last_windows_error("启动 Raw Input 前台状态计时器")
            now = datetime.now(timezone.utc)
            self._update_focus(user32, now, time.perf_counter())
            self._ready.set()
            message = _MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise _last_windows_error("读取 Raw Input 消息")
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            if devices_registered:
                self._unregister_devices(user32)
            if created_hwnd and user32.IsWindow(created_hwnd):
                user32.DestroyWindow(created_hwnd)
            self._window_handle = 0
            user32.UnregisterClassW(class_name, instance)
        if self._thread_error is not None:
            raise self._thread_error

    def _register_devices(self, user32: object, hwnd: int) -> None:
        devices = (_RAWINPUTDEVICE * 2)(
            _RAWINPUTDEVICE(
                _HID_USAGE_PAGE_GENERIC,
                _HID_USAGE_GENERIC_MOUSE,
                _RIDEV_INPUTSINK,
                hwnd,
            ),
            _RAWINPUTDEVICE(
                _HID_USAGE_PAGE_GENERIC,
                _HID_USAGE_GENERIC_KEYBOARD,
                _RIDEV_INPUTSINK,
                hwnd,
            ),
        )
        if not user32.RegisterRawInputDevices(
            devices,
            len(devices),
            ctypes.sizeof(_RAWINPUTDEVICE),
        ):
            raise _last_windows_error("注册 Windows Raw Input 设备")

    def _unregister_devices(self, user32: object) -> None:
        devices = (_RAWINPUTDEVICE * 2)(
            _RAWINPUTDEVICE(
                _HID_USAGE_PAGE_GENERIC,
                _HID_USAGE_GENERIC_MOUSE,
                _RIDEV_REMOVE,
                None,
            ),
            _RAWINPUTDEVICE(
                _HID_USAGE_PAGE_GENERIC,
                _HID_USAGE_GENERIC_KEYBOARD,
                _RIDEV_REMOVE,
                None,
            ),
        )
        if not user32.RegisterRawInputDevices(
            devices,
            len(devices),
            ctypes.sizeof(_RAWINPUTDEVICE),
        ):
            raise _last_windows_error("注销 Windows Raw Input 设备")

    def _receive_raw_input(self, user32: object, raw_handle: int) -> None:
        size = ctypes.c_uint32()
        header_size = ctypes.sizeof(_RAWINPUTHEADER)
        result = user32.GetRawInputData(
            raw_handle,
            _RID_INPUT,
            None,
            ctypes.byref(size),
            header_size,
        )
        if result == 0xFFFFFFFF:
            raise _last_windows_error("读取 Raw Input 大小")
        buffer = ctypes.create_string_buffer(size.value)
        result = user32.GetRawInputData(
            raw_handle,
            _RID_INPUT,
            buffer,
            ctypes.byref(size),
            header_size,
        )
        if result == 0xFFFFFFFF or result != size.value:
            raise _last_windows_error("读取 Raw Input 数据")
        raw = ctypes.cast(buffer, ctypes.POINTER(_RAWINPUT)).contents
        timestamp = datetime.now(timezone.utc)
        monotonic = time.perf_counter()
        foreground = self._update_focus(user32, timestamp, monotonic)
        if raw.header.dwType == _RIM_TYPEKEYBOARD:
            self._record_keyboard(
                user32,
                raw.keyboard,
                timestamp,
                monotonic,
                foreground,
            )
        elif raw.header.dwType == _RIM_TYPEMOUSE:
            self._record_mouse(raw.mouse, timestamp, monotonic, foreground)

    def _record_keyboard(
        self,
        user32: object,
        keyboard: _RAWKEYBOARD,
        timestamp: datetime,
        monotonic_seconds: float,
        foreground: bool,
    ) -> None:
        del user32
        name = _VIRTUAL_KEY_NAMES.get(int(keyboard.VKey))
        if name is None:
            return
        self._recorder.processor.process(
            InputSample(
                timestamp,
                "key",
                foreground,
                name=name,
                pressed=not bool(keyboard.Flags & _RI_KEY_BREAK),
                monotonic_seconds=monotonic_seconds,
            )
        )

    def _record_mouse(
        self,
        mouse: _RAWMOUSE,
        timestamp: datetime,
        monotonic_seconds: float,
        foreground: bool,
    ) -> None:
        relative = not bool(mouse.usFlags & _MOUSE_MOVE_ABSOLUTE)
        self._recorder.processor.process(
            InputSample(
                timestamp,
                "move",
                foreground,
                dx=int(mouse.lLastX),
                dy=int(mouse.lLastY),
                relative=relative,
                monotonic_seconds=monotonic_seconds,
            )
        )
        flags = int(mouse.usButtonFlags)
        button_flags = (
            (_RI_MOUSE_LEFT_BUTTON_DOWN, "MouseLeft", True),
            (_RI_MOUSE_LEFT_BUTTON_UP, "MouseLeft", False),
            (_RI_MOUSE_RIGHT_BUTTON_DOWN, "MouseRight", True),
            (_RI_MOUSE_RIGHT_BUTTON_UP, "MouseRight", False),
            (_RI_MOUSE_MIDDLE_BUTTON_DOWN, "MouseMiddle", True),
            (_RI_MOUSE_MIDDLE_BUTTON_UP, "MouseMiddle", False),
            (_RI_MOUSE_BUTTON_4_DOWN, "Mouse4", True),
            (_RI_MOUSE_BUTTON_4_UP, "Mouse4", False),
            (_RI_MOUSE_BUTTON_5_DOWN, "Mouse5", True),
            (_RI_MOUSE_BUTTON_5_UP, "Mouse5", False),
        )
        for flag, name, pressed in button_flags:
            if flags & flag:
                self._recorder.processor.process(
                    InputSample(
                        timestamp,
                        "mouse_button",
                        foreground,
                        name=name,
                        pressed=pressed,
                        monotonic_seconds=monotonic_seconds,
                    )
                )
        if flags & _RI_MOUSE_WHEEL:
            delta = ctypes.c_int16(mouse.usButtonData).value
            if delta:
                self._recorder.processor.process(
                    InputSample(
                        timestamp,
                        "wheel",
                        foreground,
                        name="WheelUp" if delta > 0 else "WheelDown",
                        monotonic_seconds=monotonic_seconds,
                    )
                )

    def _update_focus(
        self,
        user32: object,
        timestamp: datetime,
        monotonic_seconds: float,
    ) -> bool:
        foreground = int(user32.GetForegroundWindow() or 0) == self._target_hwnd
        self._recorder.processor.set_foreground(
            foreground,
            timestamp,
            monotonic_seconds,
        )
        return foreground

    @staticmethod
    def _configure_functions(user32: object, kernel32: object) -> None:
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetKeyNameTextW.argtypes = [
            ctypes.c_int32,
            ctypes.c_wchar_p,
            ctypes.c_int32,
        ]
        user32.GetKeyNameTextW.restype = ctypes.c_int32
        user32.IsWindow.argtypes = [ctypes.c_void_p]
        user32.IsWindow.restype = ctypes.c_bool
        user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
        user32.RegisterClassW.restype = ctypes.c_uint16
        user32.UnregisterClassW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
        user32.UnregisterClassW.restype = ctypes.c_bool
        user32.CreateWindowExW.argtypes = [
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.DefWindowProcW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DestroyWindow.argtypes = [ctypes.c_void_p]
        user32.DestroyWindow.restype = ctypes.c_bool
        user32.PostQuitMessage.argtypes = [ctypes.c_int32]
        user32.PostQuitMessage.restype = None
        user32.RegisterRawInputDevices.argtypes = [
            ctypes.POINTER(_RAWINPUTDEVICE),
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        user32.RegisterRawInputDevices.restype = ctypes.c_bool
        user32.GetRawInputData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32,
        ]
        user32.GetRawInputData.restype = ctypes.c_uint32
        user32.SetTimer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        user32.SetTimer.restype = ctypes.c_size_t
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(_MSG),
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        user32.GetMessageW.restype = ctypes.c_int32
        user32.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
        user32.TranslateMessage.restype = ctypes.c_bool
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        user32.PostMessageW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        user32.PostMessageW.restype = ctypes.c_bool
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p


class FullKeyboardWindowsRawInputBackend(WindowsRawInputBackend):
    """Development-probe backend; production never constructs this class."""

    def _record_keyboard(
        self,
        user32: object,
        keyboard: _RAWKEYBOARD,
        timestamp: datetime,
        monotonic_seconds: float,
        foreground: bool,
    ) -> None:
        name = _full_keyboard_name(user32, keyboard)
        if name is None:
            return
        self._recorder.processor.process(
            InputSample(
                timestamp,
                "key",
                foreground,
                name=name,
                pressed=not bool(keyboard.Flags & _RI_KEY_BREAK),
                monotonic_seconds=monotonic_seconds,
            )
        )


class ActionInputListener:
    """Production action-only listener with no file writer or full-key mode."""

    def __init__(
        self,
        target_hwnd: int,
        *,
        retention_seconds: float = ACTION_INPUT_RETENTION_SECONDS,
        backend_factory: Callable[[int, RawInputOwnerLike], WindowsRawInputBackend]
        | None = None,
    ) -> None:
        self.timeline = ActionInputTimeline(retention_seconds)
        self.processor = ActionInputEventProcessor(self.timeline.append)
        factory = backend_factory or WindowsRawInputBackend
        self._backend = factory(target_hwnd, self)
        self._closed = False

    def summarize_window(
        self,
        start_exclusive: float | None,
        end_inclusive: float,
    ) -> str:
        return self.timeline.summarize_window(start_exclusive, end_inclusive)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._backend.close()
        finally:
            anchor = ClockAnchor.sample()
            self.processor.finish(anchor.utc, anchor.monotonic_seconds)
            self._closed = True


def load_action_input_csv(session_directory: Path) -> LoadedActionInput:
    """Load a replay CSV through the same action allowlist used in production.

    Historical probe CSVs stored absolute horizontal/vertical movement only,
    so their summaries can report the dominant axis but cannot invent a sign.
    """
    timeline = ActionInputTimeline(retention_seconds=None)
    path = session_directory / "input.csv"
    if not path.is_file():
        return LoadedActionInput(timeline, True, False)
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        fieldnames = set(reader.fieldnames or ())
        if "时间" not in fieldnames or "事件类型" not in fieldnames:
            raise InputTelemetryError(f"输入记录缺少必要列：{path}")
        has_monotonic = "单调秒" in fieldnames
        for row in reader:
            try:
                event_type = row["事件类型"]
                if event_type not in {"按下", "抬起", "滚轮", "移动汇总", "焦点丢失"}:
                    continue
                name = row.get("键名", "")
                if name and name not in INPUT_NAME_WHITELIST:
                    continue
                monotonic_text = row.get("单调秒", "") if has_monotonic else ""
                monotonic = (
                    float(monotonic_text)
                    if monotonic_text
                    else datetime.fromisoformat(row["时间"]).timestamp()
                )
                if not math.isfinite(monotonic):
                    raise ValueError("non-finite monotonic timestamp")
                if event_type == "移动汇总":
                    horizontal = abs(int(row.get("dx", "") or 0))
                    vertical = abs(int(row.get("dy", "") or 0))
                    timeline.append(
                        ActionInputEvent(
                            monotonic,
                            "移动汇总",
                            absolute_dx=horizontal,
                            absolute_dy=vertical,
                            direction_known=False,
                        )
                    )
                else:
                    timeline.append(
                        ActionInputEvent(
                            monotonic,
                            cast(InputEventType, event_type),
                            name,
                        )
                    )
            except (KeyError, TypeError, ValueError) as error:
                raise InputTelemetryError(f"无法读取输入记录 {path}：{error}") from error
    return LoadedActionInput(timeline, False, False)


def empty_input_summary() -> dict[str, object]:
    return InputStatistics().summary()


def _utc(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        raise ValueError("input timestamps must include a timezone")
    return timestamp.astimezone(timezone.utc)


def _event_monotonic(timestamp: datetime, value: float | None) -> float:
    """Keep old injectable call sites deterministic while production passes QPC."""
    return timestamp.timestamp() if value is None else value


def _percentile(ordered: list[float], percentile: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _last_windows_error(action: str) -> InputTelemetryError:
    error_code = ctypes.get_last_error()
    detail = ctypes.WinError(error_code) if error_code else "未知 Windows 错误"
    return InputTelemetryError(f"{action}失败：{detail}")
