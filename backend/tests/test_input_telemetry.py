"""Synthetic input streams test privacy filtering without real Windows input."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import numpy as np
import pytest

from pet.core.capture import AdaptiveFrameSelector, MetricsCsvWriter, ProbeOptions
from pet.core import capture, input_telemetry
from pet.core.input_telemetry import (
    INPUT_CSV_HEADER,
    INPUT_NAME_WHITELIST,
    InputEventProcessor,
    InputSample,
    InputTelemetryError,
    InputTelemetryRecorder,
    WindowsRawInputBackend,
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
    assert rows[1] == [STARTED.isoformat(), "按下", "MouseLeft", "", ""]


def test_input_and_frame_metrics_share_utc_wall_clock(tmp_path: Path) -> None:
    input_recorder = InputTelemetryRecorder(tmp_path)
    metrics_writer = MetricsCsvWriter(tmp_path)
    selector = AdaptiveFrameSelector(min_save_interval=0.0)
    observation = selector.observe(np.zeros((54, 96, 3), dtype=np.uint8), STARTED.timestamp())

    input_recorder.processor.process(
        InputSample(STARTED, "key", True, name="W", pressed=True)
    )
    metrics_writer.write(1, STARTED, "Synthetic Game", observation, "", "", 0.0)

    with (tmp_path / "input.csv").open(encoding="utf-8-sig", newline="") as handle:
        input_rows = list(csv.reader(handle))
    with (tmp_path / "metrics.csv").open(encoding="utf-8-sig", newline="") as handle:
        metric_rows = list(csv.reader(handle))
    input_recorder.close(STARTED)
    metrics_writer.close()

    assert input_rows[1][0] == metric_rows[1][1] == STARTED.isoformat()


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
