"""Synthetic input streams test privacy filtering without real Windows input."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading

import numpy as np
import pytest

from pet.core.capture import AdaptiveFrameSelector, MetricsCsvWriter, ProbeOptions
from pet.core import capture, input_telemetry
from pet.core.input_telemetry import (
    ACTION_INPUT_EMPTY_TEXT,
    ActionInputEvent,
    ActionInputEventProcessor,
    ActionInputTimeline,
    ClockAnchor,
    INPUT_CSV_HEADER,
    INPUT_NAME_WHITELIST,
    MOUSE_LARGE_MOVEMENT_UNITS,
    MOUSE_MODERATE_MOVEMENT_UNITS,
    InputEventProcessor,
    InputSample,
    InputTelemetryError,
    InputTelemetryRecorder,
    MouseAngleContext,
    WindowsRawInputBackend,
    load_action_input_csv,
)


STARTED = datetime(2026, 8, 25, 1, 2, 3, tzinfo=timezone.utc)


def test_hardcoded_privacy_allowlist_matches_v1_exactly() -> None:
    assert INPUT_NAME_WHITELIST == {
        "W", "A", "S", "D", "Shift", "Ctrl", "Alt", "Space", "Tab",
        "E", "Q", "R", "F", "C", "V", "G", "Esc",
        "F1", "F2", "F3", "F4", "F5",
        "1", "2", "3", "4", "5", "6", "7", "8", "9",
        "MouseLeft", "MouseRight", "MouseMiddle", "Mouse4", "Mouse5",
        "WheelUp", "WheelDown",
    }


def test_non_allowlisted_key_is_discarded_without_a_counter() -> None:
    records: list[input_telemetry.InputRecord] = []
    processor = InputEventProcessor(records.append)

    processor.process(InputSample(STARTED, "key", True, name="B", pressed=True))

    assert records == []
    assert processor.statistics.event_count == 0
    assert processor.statistics.press_counts == {}


def test_events_are_not_recorded_when_target_is_not_foreground() -> None:
    records: list[input_telemetry.InputRecord] = []
    processor = InputEventProcessor(records.append)

    processor.process(InputSample(STARTED, "key", False, name="W", pressed=True))
    processor.process(
        InputSample(STARTED, "move", False, dx=400, dy=-300, relative=True)
    )
    processor.finish(STARTED + timedelta(seconds=1))

    assert records == []
    assert processor.statistics.event_count == 0


def test_relative_mouse_movement_is_aggregated_by_fixed_window() -> None:
    records: list[input_telemetry.InputRecord] = []
    processor = InputEventProcessor(records.append, movement_window_seconds=0.1)

    processor.process(InputSample(STARTED, "move", True, dx=3, dy=-4))
    processor.process(
        InputSample(
            STARTED + timedelta(milliseconds=50),
            "move",
            True,
            dx=-2,
            dy=1,
        )
    )
    processor.flush_due(STARTED + timedelta(milliseconds=100))

    assert len(records) == 1
    assert records[0].event_type == "移动汇总"
    assert (records[0].dx, records[0].dy) == (5, 5)
    assert processor.statistics.movement_magnitudes == [pytest.approx(5 * 2**0.5)]


def test_absolute_mouse_coordinates_are_never_recorded() -> None:
    records: list[input_telemetry.InputRecord] = []
    processor = InputEventProcessor(records.append)

    processor.process(
        InputSample(
            STARTED,
            "move",
            True,
            dx=1920,
            dy=1080,
            relative=False,
        )
    )
    processor.finish(STARTED + timedelta(seconds=1))

    assert records == []


def test_focus_lost_is_written_once_per_focus_transition() -> None:
    records: list[input_telemetry.InputRecord] = []
    processor = InputEventProcessor(records.append)
    processor.set_foreground(True, STARTED)

    processor.set_foreground(False, STARTED + timedelta(milliseconds=10))
    processor.set_foreground(False, STARTED + timedelta(milliseconds=20))

    assert [record.event_type for record in records] == ["焦点丢失"]
    assert processor.statistics.focus_lost_count == 1


def test_session_statistics_count_presses_movement_and_focus_loss() -> None:
    records: list[input_telemetry.InputRecord] = []
    processor = InputEventProcessor(records.append)
    processor.process(InputSample(STARTED, "key", True, name="W", pressed=True))
    processor.process(
        InputSample(
            STARTED + timedelta(milliseconds=10),
            "key",
            True,
            name="W",
            pressed=False,
        )
    )
    processor.process(
        InputSample(
            STARTED + timedelta(milliseconds=20),
            "move",
            True,
            dx=3,
            dy=4,
        )
    )
    processor.set_foreground(False, STARTED + timedelta(milliseconds=30))

    summary = processor.statistics.summary()
    assert summary["事件总数"] == 4
    assert summary["按键名分组的按下次数"] == {"W": 1}
    assert summary["鼠标位移量中位"] == pytest.approx(5.0)
    assert summary["鼠标位移量P90"] == pytest.approx(5.0)
    assert summary["focus_lost次数"] == 1


def test_input_csv_header_and_row_are_immediately_flushed(tmp_path: Path) -> None:
    recorder = InputTelemetryRecorder(tmp_path)
    recorder.processor.process(
        InputSample(STARTED, "key", True, name="MouseLeft", pressed=True)
    )

    with (tmp_path / "input.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    recorder.close(STARTED + timedelta(seconds=1))

    assert tuple(rows[0]) == INPUT_CSV_HEADER
    assert rows[1] == [
        STARTED.isoformat(),
        f"{STARTED.timestamp():.9f}",
        "按下",
        "MouseLeft",
        "",
        "",
    ]


def test_input_and_frame_metrics_share_monotonic_clock(tmp_path: Path) -> None:
    input_recorder = InputTelemetryRecorder(tmp_path)
    metrics_writer = MetricsCsvWriter(tmp_path)
    selector = AdaptiveFrameSelector(min_save_interval=0.0)
    observation = selector.observe(np.zeros((54, 96, 3), dtype=np.uint8), STARTED.timestamp())

    input_recorder.processor.process(
        InputSample(
            STARTED,
            "key",
            True,
            name="W",
            pressed=True,
            monotonic_seconds=123.456,
        )
    )
    metrics_writer.write(
        1,
        STARTED,
        "Synthetic Game",
        observation,
        "",
        "",
        0.0,
        monotonic_seconds=123.456,
    )

    with (tmp_path / "input.csv").open(encoding="utf-8-sig", newline="") as handle:
        input_rows = list(csv.reader(handle))
    with (tmp_path / "metrics.csv").open(encoding="utf-8-sig", newline="") as handle:
        metric_rows = list(csv.reader(handle))
    input_recorder.close(STARTED)
    metrics_writer.close()

    assert input_rows[1][1] == metric_rows[1][2] == "123.456000000"


def test_clock_anchor_round_trip_is_consistent() -> None:
    anchor = ClockAnchor(STARTED, 50.0)
    later_wall = STARTED + timedelta(seconds=2.25)

    monotonic = anchor.monotonic_from_wall(later_wall)

    assert monotonic == pytest.approx(52.25)
    assert anchor.wall_from_monotonic(monotonic) == later_wall


def test_full_keyboard_is_explicit_and_report_stats_are_privacy_safe() -> None:
    records: list[input_telemetry.InputRecord] = []
    processor = InputEventProcessor(records.append, full_keyboard=True)
    processor.process(InputSample(STARTED, "key", True, name="B", pressed=True))

    assert records[0].name == "B"
    assert processor.statistics.summary()["按键名分组的按下次数"] == {"B": 1}
    assert processor.statistics.report_safe_press_counts() == {"其他键合计": 1}


def test_full_keyboard_writes_non_allowlisted_key_to_local_csv(tmp_path: Path) -> None:
    recorder = InputTelemetryRecorder(tmp_path, full_keyboard=True)
    recorder.processor.process(
        InputSample(
            STARTED,
            "key",
            True,
            name="B",
            pressed=True,
            monotonic_seconds=20.0,
        )
    )
    with (tmp_path / "input.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))
    recorder.close(STARTED)

    assert rows[0]["键名"] == "B"
    assert rows[0]["单调秒"] == "20.000000000"


def test_action_context_preserves_window_boundaries_clicks_holds_and_direction() -> None:
    timeline = ActionInputTimeline(retention_seconds=None)
    processor = ActionInputEventProcessor(timeline.append)

    def sample(
        seconds: float,
        kind: input_telemetry.InputSampleKind,
        *,
        name: str = "",
        pressed: bool | None = None,
        dx: int = 0,
        dy: int = 0,
    ) -> None:
        processor.process(
            InputSample(
                STARTED + timedelta(seconds=seconds),
                kind,
                True,
                name=name,
                pressed=pressed,
                dx=dx,
                dy=dy,
                monotonic_seconds=seconds,
            )
        )

    sample(1.0, "key", name="W", pressed=True)
    sample(1.1, "key", name="B", pressed=True)
    sample(1.2, "mouse_button", name="MouseLeft", pressed=True)
    sample(1.21, "mouse_button", name="MouseLeft", pressed=False)
    sample(1.35, "mouse_button", name="MouseLeft", pressed=True)
    sample(1.36, "mouse_button", name="MouseLeft", pressed=False)
    sample(1.4, "move", dx=-30, dy=-80)
    processor.flush_due(
        STARTED + timedelta(seconds=1.5),
        monotonic_seconds=1.5,
    )
    sample(2.0, "key", name="W", pressed=False)
    sample(2.1, "key", name="A", pressed=True)

    summary = timeline.summarize_window(1.0, 2.0)

    assert "W 按住 1.0 秒" in summary
    assert "左键点击 2 次（平均间隔约 0.15 秒）" in summary
    assert "鼠标向上轻微转动" in summary
    assert "B" not in summary
    assert "A" not in summary


def test_action_timeline_rejects_full_keyboard_events_before_model_summary() -> None:
    timeline = ActionInputTimeline(retention_seconds=None)
    timeline.append(ActionInputEvent(1.0, "按下", "B"))
    timeline.append(ActionInputEvent(1.1, "按下", "W"))
    timeline.append(ActionInputEvent(1.6, "抬起", "W"))

    summary = timeline.summarize_window(None, 2.0)

    assert summary == "W 按住 0.5 秒"


def test_replay_csv_filters_full_keyboard_and_marks_legacy_mouse_axis(
    tmp_path: Path,
) -> None:
    with (tmp_path / "input.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow(INPUT_CSV_HEADER)
        writer.writerow((STARTED.isoformat(), "1.000000000", "按下", "B", "", ""))
        writer.writerow((STARTED.isoformat(), "1.100000000", "按下", "W", "", ""))
        writer.writerow((STARTED.isoformat(), "1.600000000", "抬起", "W", "", ""))
        writer.writerow((STARTED.isoformat(), "1.700000000", "移动汇总", "", 800, 100))

    loaded = load_action_input_csv(tmp_path)
    summary = loaded.timeline.summarize_window(None, 2.0)

    assert loaded.csv_missing is False
    assert loaded.direction_available is False
    assert "W 按住 0.5 秒" in summary
    assert "B" not in summary
    assert "鼠标横向中等转动" in summary


@pytest.mark.parametrize(
    ("raw_count_total", "expected_magnitude"),
    [
        (MOUSE_MODERATE_MOVEMENT_UNITS - 1, "slight"),
        (MOUSE_MODERATE_MOVEMENT_UNITS, "moderate"),
        (MOUSE_LARGE_MOVEMENT_UNITS - 1, "moderate"),
        (MOUSE_LARGE_MOVEMENT_UNITS, "large"),
    ],
)
def test_mouse_motion_magnitude_boundaries(
    raw_count_total: int,
    expected_magnitude: str,
) -> None:
    timeline = ActionInputTimeline(retention_seconds=None)
    timeline.append(
        ActionInputEvent(
            1.0,
            "移动汇总",
            dx=raw_count_total,
            absolute_dx=raw_count_total,
        )
    )

    result = timeline.summarize_window_result(0.0, 1.0)

    assert result.mouse_motion is not None
    assert result.mouse_motion.magnitude == expected_magnitude
    assert result.mouse_motion.raw_count_total == raw_count_total
    assert str(raw_count_total) not in result.summary


def test_empty_mouse_window_emits_no_motion_aggregate() -> None:
    timeline = ActionInputTimeline(retention_seconds=None)

    result = timeline.summarize_window_result(0.0, 1.0)

    assert result.mouse_motion is None
    assert result.summary == ACTION_INPUT_EMPTY_TEXT


@pytest.mark.parametrize(
    ("dx", "dy", "expected_direction", "expected_text"),
    [
        (-600, 10, "left", "鼠标向左中等转动"),
        (600, 10, "right", "鼠标向右中等转动"),
        (10, -600, "up", "鼠标向上中等转动"),
        (10, 600, "down", "鼠标向下中等转动"),
    ],
)
def test_mouse_motion_preserves_four_signed_directions(
    dx: int,
    dy: int,
    expected_direction: str,
    expected_text: str,
) -> None:
    timeline = ActionInputTimeline(retention_seconds=None)
    timeline.append(
        ActionInputEvent(
            1.0,
            "移动汇总",
            dx=dx,
            dy=dy,
            absolute_dx=abs(dx),
            absolute_dy=abs(dy),
        )
    )

    result = timeline.summarize_window_result(0.0, 1.0)

    assert result.mouse_motion is not None
    assert result.mouse_motion.direction == expected_direction
    assert result.summary == expected_text


def test_absolute_angle_requires_complete_ordinary_view_context() -> None:
    timeline = ActionInputTimeline(retention_seconds=None)
    timeline.append(
        ActionInputEvent(
            1.0,
            "移动汇总",
            dx=-2045,
            absolute_dx=2045,
        )
    )
    contexts = (
        None,
        MouseAngleContext(yaw_degrees_per_count=0.022, view_mode="ordinary"),
        MouseAngleContext(user_sensitivity=2.0, view_mode="ordinary"),
        MouseAngleContext(
            yaw_degrees_per_count=0.022,
            user_sensitivity=2.0,
            view_mode="scoped",
        ),
    )
    for context in contexts:
        result = timeline.summarize_window_result(
            0.0,
            1.0,
            angle_context=context,
        )
        assert result.mouse_motion is not None
        assert result.mouse_motion.estimated_degrees is None
        assert "°" not in result.summary

    calibrated = timeline.summarize_window_result(
        0.0,
        1.0,
        angle_context=MouseAngleContext(
            yaw_degrees_per_count=0.022,
            user_sensitivity=2.0,
            view_mode="ordinary",
        ),
    )
    assert calibrated.mouse_motion is not None
    assert calibrated.mouse_motion.estimated_degrees == pytest.approx(89.98)
    assert calibrated.summary == "鼠标向左大幅转动（约 90°）"


def test_replay_csv_can_align_input_to_legacy_wall_clock(tmp_path: Path) -> None:
    wall = STARTED + timedelta(seconds=2)
    with (tmp_path / "input.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow(INPUT_CSV_HEADER)
        writer.writerow((wall.isoformat(), "123.0", "移动汇总", "", 600, 0))

    loaded = load_action_input_csv(tmp_path, use_wall_clock=True)
    result = loaded.timeline.summarize_window_result(
        wall.timestamp() - 0.1,
        wall.timestamp(),
    )

    assert result.mouse_motion is not None
    assert result.mouse_motion.direction == "horizontal"
    assert result.mouse_motion.magnitude == "moderate"
    assert result.mouse_motion.estimated_degrees is None
    assert result.mouse_motion.raw_count_total == 600


def test_missing_replay_csv_produces_explicit_no_input_signal(tmp_path: Path) -> None:
    loaded = load_action_input_csv(tmp_path)

    assert loaded.csv_missing is True
    assert loaded.timeline.summarize_window(None, 10.0) == ACTION_INPUT_EMPTY_TEXT


def test_session_json_receives_input_statistics(tmp_path: Path) -> None:
    recorder = InputTelemetryRecorder(tmp_path)
    recorder.processor.process(
        InputSample(STARTED, "key", True, name="W", pressed=True)
    )
    recorder.close(STARTED + timedelta(seconds=1))
    options = ProbeOptions(
        1.0,
        "Game",
        tmp_path,
        record_all=True,
        record_input=True,
    )

    capture._write_session(
        options,
        STARTED,
        STARTED + timedelta(seconds=1),
        1,
        {"落盘次数": 1},
        recorder.summary(),
    )

    payload = json.loads((tmp_path / "session.json").read_text(encoding="utf-8"))
    assert payload["启动参数"]["record_all"] is True
    assert payload["输入记录"]["已开启"] is True
    assert payload["输入记录"]["事件总数"] == 1
    assert payload["输入记录"]["按键名分组的按下次数"] == {"W": 1}
    assert "perf_counter秒" in payload["时钟锚点"]
    assert "对应UTC墙钟" in payload["时钟锚点"]


def test_non_windows_backend_has_human_readable_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder = InputTelemetryRecorder(tmp_path)
    monkeypatch.setattr(input_telemetry.sys, "platform", "linux")
    try:
        with pytest.raises(InputTelemetryError, match="只支持 Windows"):
            WindowsRawInputBackend(123, recorder)
    finally:
        recorder.close(STARTED)


# 锁住初始化超时后构造失败对象遗留 Raw Input 监听线程的故障。
def test_windows_backend_timeout_stops_late_initialization_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder = InputTelemetryRecorder(tmp_path)
    backends: list[WindowsRawInputBackend] = []

    class FakeUser32:
        def PostMessageW(self, _handle: int, _message: int, _wparam: int, _lparam: int) -> None:
            return None

    def delayed_message_loop(self: WindowsRawInputBackend) -> None:
        backends.append(self)
        self._user32 = FakeUser32()
        self._window_handle = 123
        self._stop_requested.wait()

    monkeypatch.setattr(input_telemetry.sys, "platform", "win32")
    monkeypatch.setattr(
        WindowsRawInputBackend,
        "_message_loop",
        delayed_message_loop,
    )
    try:
        with pytest.raises(InputTelemetryError, match="超时"):
            WindowsRawInputBackend(123, recorder, ready_timeout_seconds=0.2)
        assert len(backends) == 1
        assert backends[0]._thread.is_alive() is False
    finally:
        recorder.close(STARTED)


# 锁住初始化超时且线程拒绝停止时错误信息未暴露隐私监听仍存活的故障。
def test_windows_backend_timeout_reports_listener_thread_still_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder = InputTelemetryRecorder(tmp_path)
    release = threading.Event()
    backends: list[WindowsRawInputBackend] = []

    class FakeUser32:
        def PostMessageW(self, _handle: int, _message: int, _wparam: int, _lparam: int) -> None:
            return None

    def blocked_message_loop(self: WindowsRawInputBackend) -> None:
        backends.append(self)
        self._user32 = FakeUser32()
        self._window_handle = 456
        release.wait()

    monkeypatch.setattr(input_telemetry.sys, "platform", "win32")
    monkeypatch.setattr(
        WindowsRawInputBackend,
        "_message_loop",
        blocked_message_loop,
    )
    try:
        with pytest.raises(InputTelemetryError, match="监听线程仍在运行"):
            WindowsRawInputBackend(123, recorder, ready_timeout_seconds=0.2)
    finally:
        release.set()
        if backends:
            backends[0]._thread.join(timeout=1.0)
        recorder.close(STARTED)
