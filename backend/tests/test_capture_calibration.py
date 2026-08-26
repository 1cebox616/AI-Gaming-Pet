"""Synthetic-stream tests for the M5 offline calibration tools."""

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
    _correlation_peak_lag,
    _result_bytes,
    default_grid,
    evaluate_session,
    load_segments,
    measure_crosscorrelation,
    prepare_session,
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

    assert len(combinations) == 3 * 6 * 6 * 1 == 108
    assert {parameters.persistence_polls for parameters in combinations} == {1}
    assert min(parameters.noise_multiplier for parameters in combinations) == 0.8
    assert min(parameters.noise_margin_pixels for parameters in combinations) == 0.0
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


def test_segments_filter_synthetic_monotonic_stream_read_only(tmp_path: Path) -> None:
    session = tmp_path / "session"
    raw = session / "raw"
    raw.mkdir(parents=True)
    started = datetime(2026, 8, 25, tzinfo=timezone.utc)
    for index, value in enumerate((10, 20, 30, 40), start=1):
        stamp = (started + timedelta(seconds=index - 1)).strftime("%Y%m%dT%H%M%S.%fZ")
        _frame(value).save(raw / f"raw-{index:06d}-{stamp}.jpg", quality=70)
    with (session / "metrics.csv").open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.writer(target)
        writer.writerow(("序号", "时间", "单调秒", "时间来源"))
        writer.writerow((0, started.isoformat(), "", "capture"))  # WGC empty poll
        for index in range(1, 5):
            writer.writerow(
                (
                    index,
                    (started + timedelta(seconds=index - 1)).isoformat(),
                    99.0 + index,
                    "capture",
                )
            )
    with (session / "input.csv").open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.writer(target)
        writer.writerow(("时间", "单调秒", "事件类型", "键名", "dx", "dy"))
        writer.writerow(((started + timedelta(milliseconds=500)).isoformat(), 100.5, "按下", "W", 0, 0))
        writer.writerow(((started + timedelta(seconds=1, milliseconds=500)).isoformat(), 101.5, "按下", "W", 0, 0))
    (session / "session.json").write_text(
        '{"时钟锚点":{"perf_counter秒":99.75}}', encoding="utf-8"
    )
    manifest = tmp_path / "segments.toml"
    manifest.write_text(
        "[[segments]]\nrole='slice'\nsession='session'\nstart=1.0\nend=3.0\n",
        encoding="utf-8",
    )

    spec = load_segments(manifest)[0]
    prepared = prepare_session(
        spec.role,
        spec.session_dir,
        sample_stride=1,
        strong_block_delta=40.0 / 255.0,
        input_motion_threshold=20.0,
        start_seconds=spec.start_seconds,
        end_seconds=spec.end_seconds,
    )

    scoped_paths = tuple(
        path.name.split("-")[1]
        for path, in_scope in zip(prepared.paths, prepared.in_scope, strict=True)
        if in_scope
    )
    scoped_times = tuple(
        value
        for value, in_scope in zip(
            prepared.relative_seconds, prepared.in_scope, strict=True
        )
        if in_scope
    )
    scoped_input = tuple(
        active
        for active, in_scope in zip(
            prepared.input_activity or (), prepared.in_scope, strict=True
        )
        if in_scope
    )
    assert scoped_paths == ("000002", "000003")
    assert scoped_times == pytest.approx((1.25, 2.25))
    assert scoped_input == (True, True)
    assert len(tuple(raw.glob("*.jpg"))) == 4
    replayed = capture.run_replay(
        capture.ProbeOptions(
            interval=1.0,
            title=None,
            save_dir=tmp_path / "replay-output",
            replay_dir=raw,
            segments_path=manifest,
        )
    )
    assert len(replayed) == 2
    correlation = measure_crosscorrelation(
        spec, strong_block_delta=40.0 / 255.0
    )
    assert correlation.frame_difference_count == 3
    assert correlation.input_event_count == 1


def test_cross_correlation_recovers_known_synthetic_lag() -> None:
    rng = np.random.default_rng(20260825)
    input_times = np.arange(0.05, 30.0, 0.1)
    input_values = rng.uniform(0.0, 5.0, len(input_times))
    frame_times = np.arange(5.0, 26.0, 1.0)
    expected_lag = 2.3
    frame_values = np.asarray(
        [
            np.sum(
                input_values[
                    (input_times > frame_time - expected_lag - 1.0)
                    & (input_times <= frame_time - expected_lag)
                ]
            )
            for frame_time in frame_times
        ]
    )

    peak = _correlation_peak_lag(
        input_times,
        input_values,
        frame_times,
        frame_values,
    )

    assert peak is not None
    assert peak[0] == pytest.approx(expected_lag, abs=0.11)
    assert peak[1] > 0.99


def test_cli_accepts_segments_without_positional_sessions() -> None:
    arguments = capture._build_parser().parse_args(
        ["--calibrate", "--segments", "segments.toml"]
    )

    assert arguments.calibrate == []
    assert arguments.segments == Path("segments.toml")

    replay_arguments = capture._build_parser().parse_args(
        [
            "--replay",
            "session/raw",
            "--segments",
            "segments.toml",
            "--segment-role",
            "A-world",
        ]
    )
    assert replay_arguments.segment_role == "A-world"
