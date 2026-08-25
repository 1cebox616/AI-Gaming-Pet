"""Synthetic-image tests for single-window capture and adaptive selection."""

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
    AdaptiveFrameSelector,
    CaptureArchive,
    CapturedFrame,
    CaptureError,
    CSV_HEADER,
    FrameChangeDetector,
    FrameComparisonTracker,
    FrameMetadata,
    MAX_DRAINED_SOURCE_FRAMES_PER_POLL,
    MetricsCsvWriter,
    ProbeOptions,
    RawFrameArchive,
    WindowsGraphicsCaptureBackend,
    WindowTarget,
)


def _image(value: int, *, width: int = 192, height: int = 108) -> Image.Image:
    pixels = np.full((height, width, 3), value, dtype=np.uint8)
    return Image.fromarray(pixels, mode="RGB")


def _captured_frame(
    value: int,
    captured_at: datetime,
    *,
    width: int = 192,
    height: int = 108,
) -> CapturedFrame:
    bitmap = _image(value, width=width, height=height)
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


def _selector(**overrides: object) -> AdaptiveFrameSelector:
    defaults: dict[str, object] = {
        "noise_window": 20,
        "noise_multiplier": 2.5,
        "noise_margin": 4.0 / 255.0,
        "persistence_polls": 2,
        "camera_motion_ratio": 0.35,
        "min_save_interval": 0.0,
        "max_silence": 10_000.0,
    }
    defaults.update(overrides)
    return AdaptiveFrameSelector(**defaults)  # type: ignore[arg-type]


def test_identical_images_are_not_changed() -> None:
    detector = FrameChangeDetector()
    frame = _image(96)

    assert detector.difference(frame, frame.copy()) == pytest.approx(0.0)
    assert detector.has_changed(frame, frame.copy()) is False


def test_light_noise_stays_below_default_mean_threshold() -> None:
    detector = FrameChangeDetector()
    base = np.full((108, 192, 3), 120, dtype=np.uint8)
    noise = np.indices(base.shape).sum(axis=0) % 3 - 1
    noisy = np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    assert detector.difference(base, noisy) < detector.threshold
    assert detector.has_changed(base, noisy) is False


def test_scene_switch_exceeds_default_mean_threshold() -> None:
    detector = FrameChangeDetector()

    assert detector.difference(_image(0), _image(255)) == pytest.approx(1.0)
    assert detector.has_changed(_image(0), _image(255)) is True


def test_environment_motion_raises_mean_but_not_area_metrics() -> None:
    detector = FrameChangeDetector()
    base = np.full((54, 96), 120, dtype=np.uint8)
    rng = np.random.default_rng(20260823)
    noise = rng.integers(-10, 11, base.shape, dtype=np.int16)
    motion = np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    metrics = detector.compare(base, motion)

    assert metrics.mean_amplitude > 0.015
    assert metrics.changed_area == pytest.approx(0.0)
    assert metrics.block_change == pytest.approx(0.0)


def test_text_popup_raises_area_metrics_at_expected_ratio() -> None:
    detector = FrameChangeDetector()
    base = np.full((54, 96), 220, dtype=np.uint8)
    popup = base.copy()
    # 24/96 × 32/54 = 14.81%; 2% covers bilinear boundary pixels.
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

    assert 0.015 < motion_mean < 0.025
    assert 0.015 < popup_mean < 0.025


def test_six_legacy_metrics_are_still_calculated() -> None:
    detector = FrameChangeDetector()
    base = np.full((54, 96), 120, dtype=np.uint8)
    changed = base.copy()
    changed[11:43, 36:60] = 250
    selector = _selector()

    selector.observe(base, 0.0)
    observation = selector.observe(changed, 1.0)

    assert observation.comparisons.vs_previous.mean_amplitude > 0.0
    assert observation.comparisons.vs_previous.changed_area > 0.0
    assert observation.comparisons.vs_previous.block_change > 0.0
    assert observation.comparisons.vs_baseline == observation.comparisons.vs_previous
    assert detector.block_mean_differences(
        detector.prepare(base), detector.prepare(changed)
    ).shape == (16, 9)


def test_baseline_stays_fixed_until_tracker_marks_saved() -> None:
    tracker = FrameComparisonTracker(FrameChangeDetector())
    base = np.full((180, 320), 100, dtype=np.uint8)
    noisy = np.full((180, 320), 109, dtype=np.uint8)

    tracker.observe(base)
    repeated = [tracker.observe(noisy) for _ in range(3)]

    assert [item.comparisons.vs_baseline.mean_amplitude for item in repeated] == pytest.approx(
        [9 / 255] * 3
    )
    assert [item.comparisons.vs_previous.mean_amplitude for item in repeated] == pytest.approx(
        [9 / 255, 0.0, 0.0]
    )


def test_baseline_distance_grows_for_one_way_gradient() -> None:
    tracker = FrameComparisonTracker(FrameChangeDetector())
    tracker.observe(np.full((180, 320), 100, dtype=np.uint8))

    values = [
        tracker.observe(np.full((180, 320), value, dtype=np.uint8))
        .comparisons.vs_baseline.mean_amplitude
        for value in (104, 108, 112, 116)
    ]

    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_tracker_handles_aspect_ratio_change() -> None:
    tracker = FrameComparisonTracker(FrameChangeDetector())
    tracker.observe(_image(0, width=100, height=100))

    observation = tracker.observe(_image(255, width=192, height=108))

    assert observation.comparisons.vs_previous.mean_amplitude == pytest.approx(1.0)
    assert observation.comparisons.vs_baseline.changed_area == pytest.approx(1.0)


def test_environment_motion_raises_floor_and_eventually_stops_selecting() -> None:
    selector = _selector()
    base = np.full((180, 320), 120, dtype=np.uint8)
    selector.observe(base, 0.0)
    rng = np.random.default_rng(20260824)
    observations = []
    for index in range(1, 41):
        frame = base.copy()
        noise = rng.integers(-10, 11, (135, 320), dtype=np.int16)
        frame[:135, :] = np.clip(120 + noise, 0, 255).astype(np.uint8)
        observations.append(selector.observe(frame, float(index)))

    assert observations[-1].decision.floor_median > observations[0].decision.floor_median
    assert all(item.decision.reason == "no_change" for item in observations[-5:])


def test_persistent_high_contrast_change_wins_over_prior_noise() -> None:
    selector = _selector()
    base = np.full((180, 320), 100, dtype=np.uint8)
    selector.observe(base, 0.0)
    rng = np.random.default_rng(17)
    for index in range(1, 26):
        noisy = base.copy()
        noise = rng.integers(-10, 11, (60, 80), dtype=np.int16)
        noisy[:60, :80] = np.clip(100 + noise, 0, 255).astype(np.uint8)
        selector.observe(noisy, float(index))

    changed = base.copy()
    changed[:60, :80] = 240
    first = selector.observe(changed, 26.0)
    second = selector.observe(changed, 27.0)

    assert first.decision.should_save is False
    assert second.decision.reason == "persistent_change"
    assert second.decision.should_save is True
    assert second.decision.region_grid


def test_alternating_flicker_never_satisfies_persistence() -> None:
    selector = _selector()
    base = np.full((180, 320), 100, dtype=np.uint8)
    flash = base.copy()
    flash[:60, :80] = 240
    selector.observe(base, 0.0)

    observations = [
        selector.observe(flash if index % 2 else base, float(index))
        for index in range(1, 11)
    ]

    assert not any(item.decision.should_save for item in observations)
    assert {item.decision.reason for item in observations} == {"no_change"}


def test_whole_frame_motion_is_camera_motion_without_region_grid() -> None:
    selector = _selector()
    rng = np.random.default_rng(99)
    base = rng.integers(0, 256, (180, 320), dtype=np.uint8)
    moved = np.roll(base, shift=24, axis=1)
    selector.observe(base, 0.0)
    selector.observe(moved, 1.0)

    decision = selector.observe(moved, 2.0).decision

    assert decision.should_save is True
    assert decision.reason == "camera_motion"
    assert decision.changed_block_ratio >= 0.35
    assert decision.region_grid == ()


def test_min_save_interval_suppresses_persistent_change() -> None:
    selector = _selector(persistence_polls=1, min_save_interval=1.0)
    selector.observe(_image(0), 10.0)

    decision = selector.observe(_image(255), 10.5).decision

    assert decision.should_save is False
    assert decision.reason == "suppressed_min_interval"


def test_max_silence_forces_unchanged_frame() -> None:
    selector = _selector(max_silence=60.0)
    selector.observe(_image(0), 10.0)

    decision = selector.observe(_image(0), 70.0).decision

    assert decision.should_save is True
    assert decision.forced is True
    assert decision.reason == "forced"


def test_archive_saves_immediately_previous_poll_with_exact_content(tmp_path: Path) -> None:
    archive = CaptureArchive(tmp_path)
    started = datetime(2026, 8, 24, tzinfo=timezone.utc)
    previous = _captured_frame(37, started)
    current = _captured_frame(211, started + timedelta(seconds=1))

    current_path, previous_path = archive.save_pair(current, previous, 2)

    assert current_path is not None
    assert previous_path is not None
    assert previous_path.name.endswith("-prev.png")
    with Image.open(previous_path) as saved_previous:
        assert saved_previous.convert("RGB").getpixel((0, 0)) == (37, 37, 37)
    with Image.open(current_path) as saved_current:
        assert saved_current.convert("RGB").getpixel((0, 0)) == (211, 211, 211)


def _write_raw_stream(raw_dir: Path, values: tuple[int, ...]) -> tuple[Image.Image, ...]:
    archive = RawFrameArchive(raw_dir, width=96)
    started = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
    decoded: list[Image.Image] = []
    for sequence, value in enumerate(values, start=1):
        _, replay_bitmap = archive.save(
            _captured_frame(value, started + timedelta(seconds=sequence - 1)), sequence
        )
        decoded.append(replay_bitmap)
    return tuple(decoded)


def _replay_options(raw_dir: Path, output_dir: Path) -> ProbeOptions:
    return ProbeOptions(
        interval=1.0,
        title=None,
        save_dir=output_dir,
        replay_dir=raw_dir,
        min_save_interval=0.0,
        max_silence=10_000.0,
    )


def test_replay_is_byte_deterministic(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    _write_raw_stream(raw_dir, (0, 0, 80, 80, 80, 20))

    capture.run_replay(_replay_options(raw_dir, tmp_path / "first"))
    capture.run_replay(_replay_options(raw_dir, tmp_path / "second"))

    assert (tmp_path / "first" / "metrics.csv").read_bytes() == (
        tmp_path / "second" / "metrics.csv"
    ).read_bytes()


def test_online_decisions_match_replay_for_same_raw_stream(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    decoded = _write_raw_stream(raw_dir, (0, 0, 80, 80, 80, 20))
    online_selector = _selector(min_save_interval=0.0)
    online = tuple(
        online_selector.observe(frame, float(index)).decision
        for index, frame in enumerate(decoded)
    )

    replay = capture.run_replay(_replay_options(raw_dir, tmp_path / "replay"))

    assert replay == online


def test_metrics_csv_header_rows_and_immediate_flush(tmp_path: Path) -> None:
    writer = MetricsCsvWriter(tmp_path)
    observation = _selector().observe(_image(10), 0.0)
    captured_at = datetime(2026, 8, 24, tzinfo=timezone.utc)
    writer.write(1, captured_at, "Synthetic Game", observation, "frame.png", "", 3.5)

    with (tmp_path / "metrics.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))
    writer.close()

    assert tuple(rows[0]) == CSV_HEADER
    assert len(rows) == 2
    assert rows[1][0] == "1"
    assert rows[1][9:13] == ["是", "是", "frame.png", ""]
    assert rows[1][18] == "forced"
    assert rows[1][-4:] == ["是", "1", "0.000", "正常"]


def test_metrics_csv_records_unavailable_poll_and_flushes(tmp_path: Path) -> None:
    writer = MetricsCsvWriter(tmp_path)
    attempted_at = datetime(2026, 8, 24, tzinfo=timezone.utc)

    writer.write_unavailable(2, attempted_at, "Synthetic Game", 0.25)

    with (tmp_path / "metrics.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))
    writer.close()

    assert len(rows[0]) == len(rows[1]) == len(CSV_HEADER)
    assert rows[1][0:3] == ["2", attempted_at.isoformat(), "Synthetic Game"]
    assert rows[1][-4:] == ["否", "0", "0.250", "WGC 暂无新帧"]


def test_session_json_records_new_arguments(tmp_path: Path) -> None:
    options = ProbeOptions(1.0, "Game", tmp_path, label="adaptive")
    started_at = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
    ended_at = started_at + timedelta(minutes=5)

    capture._write_session(options, started_at, ended_at, 300, {"落盘次数": 4})

    payload = json.loads((tmp_path / "session.json").read_text(encoding="utf-8"))
    assert payload["标签"] == "adaptive"
    assert payload["启动参数"]["noise_window"] == 20
    assert payload["启动参数"]["persistence_polls"] == 2
    assert payload["启动参数"]["record_all"] is False
    assert payload["启动参数"]["record_input"] is False
    assert payload["输入记录"]["已开启"] is False
    assert payload["输入记录"]["白名单版本"] == "v1"
    assert payload["总轮询数"] == 300


def test_archive_removes_oldest_png_at_file_limit(tmp_path: Path) -> None:
    archive = CaptureArchive(tmp_path, max_files=2, max_bytes=10 * 1024 * 1024)
    started_at = datetime(2026, 8, 24, tzinfo=timezone.utc)

    first = archive.save(_captured_frame(10, started_at), 1)
    second = archive.save(_captured_frame(20, started_at + timedelta(seconds=1)), 2)
    third = archive.save(_captured_frame(30, started_at + timedelta(seconds=2)), 3)

    assert first is not None and second is not None and third is not None
    assert first.exists() is False
    assert second.exists() is True
    assert third.exists() is True
    assert archive.retained_count == 2


def test_archive_removes_png_that_exceeds_byte_limit(tmp_path: Path) -> None:
    archive = CaptureArchive(tmp_path, max_files=500, max_bytes=1)

    saved = archive.save(
        _captured_frame(10, datetime(2026, 8, 24, tzinfo=timezone.utc)), 1
    )

    assert saved is None
    assert archive.retained_count == 0
    assert list(tmp_path.iterdir()) == []


def test_raw_archive_has_independent_file_limit(tmp_path: Path) -> None:
    archive = RawFrameArchive(tmp_path, width=96, max_files=2, max_bytes=10_000_000)
    started_at = datetime(2026, 8, 24, tzinfo=timezone.utc)

    first, _ = archive.save(_captured_frame(10, started_at), 1)
    archive.save(_captured_frame(20, started_at + timedelta(seconds=1)), 2)
    third, _ = archive.save(_captured_frame(30, started_at + timedelta(seconds=2)), 3)

    assert first is not None and first.exists() is False
    assert third is not None and third.exists() is True
    assert archive.retained_count == 2


def test_non_windows_backend_initialization_has_human_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capture, "_is_windows", lambda: False)

    with pytest.raises(CaptureError, match="只支持 Windows 10/11"):
        WindowsGraphicsCaptureBackend()


def test_cli_defaults_and_removed_strategy() -> None:
    parser = capture._build_parser()

    arguments = parser.parse_args(["--watch"])

    assert arguments.interval == 1.0
    assert arguments.record_all is False
    assert arguments.record_input is False
    assert arguments.raw_width == 640
    assert not hasattr(arguments, "strategy")
    with pytest.raises(SystemExit):
        parser.parse_args(["--watch", "--strategy", "mean_amplitude_vs_previous"])


def test_record_input_banner_discloses_scope_and_destination(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    options = ProbeOptions(
        1.0,
        "Game",
        tmp_path,
        record_all=True,
        record_input=True,
    )

    capture._print_banner(options)

    output = capsys.readouterr().out
    assert "【键鼠输入记录已开启】" in output
    assert "W、A、S、D" in output
    assert "MouseLeft、MouseRight" in output
    assert str(tmp_path / "input.csv") in output


class _SyntheticCaptureSession:
    def __init__(self, frames: list[np.ndarray | None]) -> None:
        self.frames = frames
        self.try_grab_calls = 0

    def try_grab(self) -> np.ndarray | None:
        self.try_grab_calls += 1
        if not self.frames:
            return None
        return self.frames.pop(0)


def _backend_with_session(session: _SyntheticCaptureSession) -> WindowsGraphicsCaptureBackend:
    backend = object.__new__(WindowsGraphicsCaptureBackend)
    backend._target = WindowTarget(123, "Synthetic Game", "synthetic.exe")
    backend._session = session
    return backend


def test_backend_drains_fifo_and_returns_only_newest_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = np.full((2, 3, 4), (10, 20, 30, 255), dtype=np.uint8)
    newest = np.full((2, 3, 4), (40, 50, 60, 255), dtype=np.uint8)
    session = _SyntheticCaptureSession([first, newest, None])
    backend = _backend_with_session(session)
    monkeypatch.setattr(capture, "_configured_user32", object)
    monkeypatch.setattr(capture, "_target_from_hwnd", lambda _hwnd, _user32: backend.target)

    frame = backend.capture_frame()

    assert frame is not None
    assert frame.metadata.source_frames_drained == 2
    assert frame.bitmap.getpixel((0, 0)) == (60, 50, 40, 255)
    assert session.try_grab_calls == 3


def test_backend_returns_none_immediately_when_no_frame_is_ready() -> None:
    session = _SyntheticCaptureSession([None])
    backend = _backend_with_session(session)

    assert backend.capture_frame() is None
    assert session.try_grab_calls == 1


def test_backend_drain_has_a_hard_bound_for_hot_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame_array = np.zeros((1, 1, 4), dtype=np.uint8)

    class _HotSession(_SyntheticCaptureSession):
        def try_grab(self) -> np.ndarray:
            self.try_grab_calls += 1
            return frame_array

    session = _HotSession([])
    backend = _backend_with_session(session)
    monkeypatch.setattr(capture, "_configured_user32", object)
    monkeypatch.setattr(capture, "_target_from_hwnd", lambda _hwnd, _user32: backend.target)

    frame = backend.capture_frame()

    assert frame is not None
    assert frame.metadata.source_frames_drained == MAX_DRAINED_SOURCE_FRAMES_PER_POLL
    assert session.try_grab_calls == MAX_DRAINED_SOURCE_FRAMES_PER_POLL
