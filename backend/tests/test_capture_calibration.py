"""Synthetic-stream tests for M5-T4 offline calibration metrics."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pet.core import capture
from pet.core.capture import AdaptiveFrameSelector, FrameChangeDetector
from pet.core.capture_calibration import (
    CalibrationParameters,
    InputEvent,
    PreparedCalibrationSession,
    _align_input_events,
    _result_bytes,
    default_grid,
    evaluate_session,
    write_grid_results,
)


def _frame(value: int) -> Image.Image:
    return Image.fromarray(
        np.full((180, 320, 3), value, dtype=np.uint8), mode="RGB"
    )


def _session(
    images: tuple[Image.Image, ...],
    *,
    strong: tuple[bool, ...] | None = None,
    input_activity: tuple[bool, ...] | None = None,
) -> PreparedCalibrationSession:
    detector = FrameChangeDetector()
    started = datetime(2026, 8, 25, tzinfo=timezone.utc)
    timestamps = tuple(started + timedelta(seconds=index) for index in range(len(images)))
    return PreparedCalibrationSession(
        role="synthetic",
        session_dir=Path("synthetic-session"),
        recorded_label="synthetic",
        paths=tuple(Path(f"raw-{index}.jpg") for index in range(len(images))),
        timestamps=timestamps,
        monotonic_seconds=tuple(float(index) for index in range(len(images))),
        timeline_source="monotonic",
        frames=tuple(detector.prepare(image) for image in images),
        strong_adjacent_changes=strong or tuple(False for _ in images),
        input_available=input_activity is not None,
        input_activity=input_activity,
        input_motion=(
            None
            if input_activity is None
            else tuple(21.0 if active else 0.0 for active in input_activity)
        ),
        original_raw_count=len(images),
        sample_stride=1,
        strong_block_delta=40.0 / 255.0,
        input_motion_threshold=20.0,
    )


def _parameters() -> CalibrationParameters:
    return CalibrationParameters(
        noise_window=20,
        noise_multiplier=2.5,
        noise_margin=4.0 / 255.0,
        persistence_polls=2,
    )


def test_local_strong_change_miss_uses_independent_adjacent_proxy() -> None:
    base = _frame(20)
    popup = np.asarray(base).copy()
    popup[45:90, 80:160] = 240
    session = _session(
        (base, Image.fromarray(popup, mode="RGB"), base.copy()),
        strong=(False, True, True),
        input_activity=(False, False, False),
    )

    result = evaluate_session(session, _parameters(), combination_index=1)

    assert result.local_strong_misses == 2
    assert result.reason_count("no_change") == 2


def test_input_activity_miss_counts_key_or_large_mouse_window() -> None:
    session = _session(
        tuple(_frame(80) for _ in range(4)),
        input_activity=(False, True, False, True),
    )

    result = evaluate_session(session, _parameters(), combination_index=1)

    assert result.input_activity_misses == 2
    assert result.local_strong_misses == 0


def test_longest_silence_uses_wall_clock_duration() -> None:
    session = _session(
        tuple(_frame(100) for _ in range(6)),
        input_activity=tuple(False for _ in range(6)),
    )

    result = evaluate_session(session, _parameters(), combination_index=1)

    assert result.longest_silence_seconds == pytest.approx(5.0)


def test_missing_input_csv_is_unavailable_not_zero(tmp_path: Path) -> None:
    session = _session(tuple(_frame(100) for _ in range(3)))
    result = evaluate_session(session, _parameters(), combination_index=1)

    assert result.input_available is False
    assert result.input_activity_misses is None
    path = tmp_path / "grid-results.csv"
    write_grid_results(path, (result,))
    with path.open(encoding="utf-8-sig", newline="") as source:
        row = next(csv.DictReader(source))
    assert row["input_csv可用"] == "否"
    assert row["输入活动漏检"] == ""


def test_input_rows_align_to_poll_windows_with_motion_sum() -> None:
    started = datetime(2026, 8, 25, tzinfo=timezone.utc)
    timestamps = (started, started + timedelta(seconds=1), started + timedelta(seconds=2))
    events = (
        InputEvent(started + timedelta(milliseconds=200), "移动汇总", 12, 0),
        InputEvent(started + timedelta(milliseconds=300), "移动汇总", 12, 0),
        InputEvent(started + timedelta(seconds=1, milliseconds=200), "按下", 0, 0),
    )

    activity, motion = _align_input_events(
        events,
        timestamps,
        tuple(timestamp.timestamp() for timestamp in timestamps),
        use_monotonic=False,
        input_motion_threshold=20.0,
    )

    assert activity == (False, True, True)
    assert motion == pytest.approx((0.0, 24.0, 0.0))


def test_input_rows_use_monotonic_timeline_when_available() -> None:
    started = datetime(2026, 8, 25, tzinfo=timezone.utc)
    timestamps = (started, started + timedelta(seconds=100))
    monotonic = (10.0, 11.0)
    events = (
        InputEvent(
            started + timedelta(seconds=50),
            "按下",
            0,
            0,
            monotonic_seconds=10.5,
        ),
    )

    activity, _ = _align_input_events(
        events,
        timestamps,
        monotonic,
        use_monotonic=True,
        input_motion_threshold=20.0,
    )

    assert activity == (False, True)


def test_default_grid_has_all_108_combinations_without_removed_dimension() -> None:
    combinations = default_grid().combinations()

    assert len(combinations) == 3 * 4 * 3 * 3 == 108
    assert all(
        not hasattr(parameters, "camera_motion_ratio")
        for parameters in combinations
    )


def test_calibration_result_is_byte_deterministic() -> None:
    session = _session(
        (_frame(10), _frame(10), _frame(220), _frame(220)),
        strong=(False, False, True, False),
        input_activity=(False, False, True, False),
    )

    first = evaluate_session(session, _parameters(), combination_index=1)
    second = evaluate_session(session, _parameters(), combination_index=1)

    assert _result_bytes(first) == _result_bytes(second)


def test_prepared_selector_path_matches_regular_observe() -> None:
    images = (_frame(20), _frame(20), _frame(220), _frame(220))
    regular = AdaptiveFrameSelector(min_save_interval=0.0, max_silence=10_000.0)
    prepared = AdaptiveFrameSelector(min_save_interval=0.0, max_silence=10_000.0)
    detector = FrameChangeDetector()

    regular_decisions = tuple(
        regular.observe(image, float(index)).decision
        for index, image in enumerate(images)
    )
    prepared_decisions = tuple(
        prepared.observe_prepared(detector.prepare(image), float(index))
        for index, image in enumerate(images)
    )

    assert prepared_decisions == regular_decisions


def test_cli_accepts_calibration_sessions_and_stride() -> None:
    arguments = capture._build_parser().parse_args(
        ["--calibrate", "text-heavy=session-a", "explore-3a=session-b", "--sample-stride", "2"]
    )

    assert arguments.calibrate == ["text-heavy=session-a", "explore-3a=session-b"]
    assert arguments.sample_stride == 2
