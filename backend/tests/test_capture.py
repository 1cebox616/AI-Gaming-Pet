"""Synthetic-image tests for single-window capture infrastructure."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pet.core import capture
from pet.core.capture import (
    CaptureArchive,
    CapturedFrame,
    CaptureError,
    CSV_HEADER,
    FrameChangeDetector,
    FrameComparisonTracker,
    FrameComparisons,
    FrameMetrics,
    FrameMetadata,
    MetricsCsvWriter,
    ProbeOptions,
    SavePolicy,
    WindowsGraphicsCaptureBackend,
)


def _image(value: int, *, width: int = 192, height: int = 108) -> Image.Image:
    pixels = np.full((height, width, 3), value, dtype=np.uint8)
    return Image.fromarray(pixels, mode="RGB")


def _captured_frame(value: int, captured_at: datetime) -> CapturedFrame:
    bitmap = _image(value)
    return CapturedFrame(
        bitmap=bitmap,
        metadata=FrameMetadata(
            window_title="Synthetic Game",
            process_name="synthetic.exe",
            captured_at=captured_at,
            width=bitmap.width,
            height=bitmap.height,
        ),
    )


def test_identical_images_are_not_changed() -> None:
    detector = FrameChangeDetector()
    frame = _image(96)

    assert detector.difference(frame, frame.copy()) == pytest.approx(0.0)
    assert detector.has_changed(frame, frame.copy()) is False


def test_light_noise_stays_below_default_threshold() -> None:
    detector = FrameChangeDetector()
    base = np.full((108, 192, 3), 120, dtype=np.uint8)
    noise = np.indices(base.shape).sum(axis=0) % 3 - 1
    noisy = np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    difference = detector.difference(base, noisy)

    assert difference < detector.threshold
    assert detector.has_changed(base, noisy) is False


def test_scene_switch_exceeds_default_threshold() -> None:
    detector = FrameChangeDetector()

    difference = detector.difference(_image(0), _image(255))

    assert difference == pytest.approx(1.0)
    assert detector.has_changed(_image(0), _image(255)) is True


def test_environment_motion_raises_mean_but_not_area_metrics() -> None:
    detector = FrameChangeDetector()
    base = np.full((54, 96), 120, dtype=np.uint8)
    rng = np.random.default_rng(20260823)
    noise = rng.integers(-10, 11, base.shape, dtype=np.int16)
    low_amplitude_motion = np.clip(base.astype(np.int16) + noise, 0, 255).astype(
        np.uint8
    )

    metrics = detector.compare(base, low_amplitude_motion)

    assert metrics.mean_amplitude > 0.015
    assert metrics.changed_area == pytest.approx(0.0)
    assert metrics.block_change == pytest.approx(0.0)


def test_text_popup_raises_area_metrics_at_expected_area_ratio() -> None:
    detector = FrameChangeDetector()
    base = np.full((54, 96), 220, dtype=np.uint8)
    popup = base.copy()
    # 24/96 * 32/54 = 14.81%. A 2% tolerance allows for the bilinear boundary
    # introduced while this 96px fixture is enlarged to area_width=320.
    popup[11:43, 36:60] = 250

    metrics = detector.compare(base, popup)

    assert metrics.changed_area == pytest.approx(0.15, abs=0.02)
    assert metrics.block_change > 0.08


def test_mean_amplitude_cannot_separate_motion_from_text_popup() -> None:
    detector = FrameChangeDetector()
    base = np.full((54, 96), 120, dtype=np.uint8)
    rng = np.random.default_rng(20260823)
    noise = rng.integers(-10, 11, base.shape, dtype=np.int16)
    motion = np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    text_base = np.full((54, 96), 220, dtype=np.uint8)
    popup = text_base.copy()
    popup[11:43, 36:60] = 250

    motion_mean = detector.compare(base, motion).mean_amplitude
    popup_mean = detector.compare(text_base, popup).mean_amplitude

    shared_ambiguous_interval = (0.015, 0.025)
    assert shared_ambiguous_interval[0] < motion_mean < shared_ambiguous_interval[1]
    assert shared_ambiguous_interval[0] < popup_mean < shared_ambiguous_interval[1]


def test_baseline_stays_fixed_until_a_frame_is_saved() -> None:
    tracker = FrameComparisonTracker(FrameChangeDetector())
    base = np.full((180, 320), 100, dtype=np.uint8)
    noisy = np.full((180, 320), 109, dtype=np.uint8)

    tracker.observe(base)
    repeated = [tracker.observe(noisy) for _ in range(3)]

    baseline_values = [item.comparisons.vs_baseline.mean_amplitude for item in repeated]
    previous_values = [item.comparisons.vs_previous.mean_amplitude for item in repeated]
    assert baseline_values == pytest.approx([9 / 255] * 3)
    assert previous_values == pytest.approx([9 / 255, 0.0, 0.0])


def test_baseline_distance_grows_monotonically_for_one_way_gradient() -> None:
    tracker = FrameComparisonTracker(FrameChangeDetector())
    tracker.observe(np.full((180, 320), 100, dtype=np.uint8))

    baseline_values = [
        tracker.observe(np.full((180, 320), value, dtype=np.uint8))
        .comparisons.vs_baseline.mean_amplitude
        for value in (104, 108, 112, 116)
    ]

    assert baseline_values == sorted(baseline_values)
    assert len(set(baseline_values)) == len(baseline_values)


def test_tracker_handles_window_aspect_ratio_change() -> None:
    tracker = FrameComparisonTracker(FrameChangeDetector())
    tracker.observe(_image(0, width=100, height=100))

    observation = tracker.observe(_image(255, width=192, height=108))

    assert observation.comparisons.vs_previous.mean_amplitude == pytest.approx(1.0)
    assert observation.comparisons.vs_baseline.changed_area == pytest.approx(1.0)


def _comparisons(value: float) -> FrameComparisons:
    metrics = FrameMetrics(value, value, value)
    return FrameComparisons(vs_previous=metrics, vs_baseline=metrics)


def test_min_save_interval_blocks_a_changed_frame() -> None:
    policy = SavePolicy("mean_amplitude_vs_previous", 0.02, 1.0, 60.0)
    assert policy.decide(_comparisons(0.0), 10.0, is_first=True).should_save
    policy.mark_saved(10.0)

    decision = policy.decide(_comparisons(0.5), 10.5)

    assert decision.should_save is False
    assert decision.forced is False


def test_max_silence_forces_an_unchanged_frame() -> None:
    policy = SavePolicy("mean_amplitude_vs_previous", 0.02, 1.0, 60.0)
    policy.mark_saved(10.0)

    decision = policy.decide(_comparisons(0.0), 70.0)

    assert decision.should_save is True
    assert decision.forced is True


def test_metrics_csv_header_rows_and_immediate_flush(tmp_path: Path) -> None:
    writer = MetricsCsvWriter(tmp_path)
    captured_at = datetime(2026, 8, 23, tzinfo=timezone.utc)
    writer.write(
        1,
        captured_at,
        "Synthetic Game",
        _comparisons(0.25),
        True,
        False,
        "frame.png",
        3.5,
    )

    # The writer deliberately remains open: this read proves each row was flushed.
    with (tmp_path / "metrics.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))
    writer.close()

    assert tuple(rows[0]) == CSV_HEADER
    assert len(rows) == 2
    assert rows[1][0] == "1"
    assert rows[1][9:12] == ["是", "否", "frame.png"]


def test_session_json_records_label_arguments_times_and_poll_count(
    tmp_path: Path,
) -> None:
    options = ProbeOptions(2.0, 0.02, "Game", tmp_path, label="3A-fullscreen")
    started_at = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
    ended_at = started_at + timedelta(minutes=5)

    capture._write_session(options, started_at, ended_at, 150, {"落盘次数": 4})

    payload = json.loads((tmp_path / "session.json").read_text(encoding="utf-8"))
    assert payload["标签"] == "3A-fullscreen"
    assert payload["启动参数"]["strategy"] == "mean_amplitude_vs_previous"
    assert payload["开始时间"] == started_at.isoformat()
    assert payload["结束时间"] == ended_at.isoformat()
    assert payload["总轮询数"] == 150
    assert payload["汇总"] == {"落盘次数": 4}


def test_default_policy_matches_m5_t1_save_timing() -> None:
    detector = FrameChangeDetector()
    tracker = FrameComparisonTracker(detector)
    policy = SavePolicy("mean_amplitude_vs_previous", 0.02, 1.0, 60.0)
    frames = [_image(value) for value in (0, 1, 10, 11, 30)]

    legacy_saved: list[int] = []
    previous: Image.Image | None = None
    for index, frame in enumerate(frames):
        changed = previous is None or _m5_t1_mean_amplitude(previous, frame) > 0.02
        if changed:
            legacy_saved.append(index)
        previous = frame

    new_saved: list[int] = []
    for index, frame in enumerate(frames):
        observation = tracker.observe(frame)
        now = float(index * 2)
        decision = policy.decide(
            observation.comparisons, now, is_first=observation.is_first
        )
        if decision.should_save:
            new_saved.append(index)
            policy.mark_saved(now)
            tracker.mark_saved(observation.prepared)

    assert new_saved == legacy_saved


def _m5_t1_mean_amplitude(previous: Image.Image, current: Image.Image) -> float:
    """Frozen copy of M5-T1's original calculation for compatibility testing."""
    previous_gray = previous.convert("L")
    current_gray = current.convert("L")
    aspect_ratio = min(
        previous_gray.height / previous_gray.width,
        current_gray.height / current_gray.width,
    )
    size = (96, max(1, round(96 * aspect_ratio)))
    previous_small = np.asarray(
        previous_gray.resize(size, Image.Resampling.BILINEAR), dtype=np.float32
    )
    current_small = np.asarray(
        current_gray.resize(size, Image.Resampling.BILINEAR), dtype=np.float32
    )
    return float(np.mean(np.abs(previous_small - current_small)) / 255.0)


def test_archive_removes_oldest_png_at_file_limit(tmp_path: Path) -> None:
    archive = CaptureArchive(tmp_path, max_files=2, max_bytes=10 * 1024 * 1024)
    started_at = datetime(2026, 8, 23, tzinfo=timezone.utc)

    first = archive.save(_captured_frame(10, started_at), 1)
    second = archive.save(_captured_frame(20, started_at + timedelta(seconds=1)), 2)
    third = archive.save(_captured_frame(30, started_at + timedelta(seconds=2)), 3)

    assert first is not None
    assert second is not None
    assert third is not None
    assert first.exists() is False
    assert second.exists() is True
    assert third.exists() is True
    assert archive.retained_count == 2


def test_archive_removes_png_that_exceeds_byte_limit(tmp_path: Path) -> None:
    archive = CaptureArchive(tmp_path, max_files=500, max_bytes=1)

    saved = archive.save(
        _captured_frame(10, datetime(2026, 8, 23, tzinfo=timezone.utc)),
        1,
    )

    assert saved is None
    assert archive.retained_count == 0
    assert list(tmp_path.iterdir()) == []


def test_non_windows_backend_initialization_has_human_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capture, "_is_windows", lambda: False)

    with pytest.raises(CaptureError, match="只支持 Windows 10/11"):
        WindowsGraphicsCaptureBackend()
