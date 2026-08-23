"""Synthetic-image tests for single-window capture infrastructure."""

from __future__ import annotations

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
    FrameChangeDetector,
    FrameMetadata,
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
