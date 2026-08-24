"""M5-T2.6 region/crop generation uses only synthetic local images."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from pet.core.capture import FrameChangeDetector
from pet.games.generic.eval.region_assets import (
    crop_bounds_for_grid,
    changed_region_grid,
    generate_region_assets,
)


def _synthetic_pair() -> tuple[Image.Image, Image.Image]:
    previous = np.zeros((160, 90), dtype=np.uint8)
    current = previous.copy()
    current[20:30, 40:50] = 255
    return Image.fromarray(previous, mode="L"), Image.fromarray(current, mode="L")


def test_changed_region_grid_uses_detector_block_threshold() -> None:
    previous, current = _synthetic_pair()
    detector = FrameChangeDetector(
        target_width=90,
        area_width=90,
        block_grid=(9, 16),
    )

    assert changed_region_grid(detector, previous, current) == ("r3c5",)


def test_crop_bounds_are_grid_envelope_plus_fixed_two_percent_margin() -> None:
    assert crop_bounds_for_grid((900, 1600), ("r3c5",)) == (382, 168, 518, 332)


def test_generator_writes_mechanical_crop_for_adjacent_frame(tmp_path: Path) -> None:
    previous, current = _synthetic_pair()
    previous_path = tmp_path / "frame-000001-20260101T000000.000000Z.png"
    current_path = tmp_path / "frame-000002-20260101T000002.000000Z.png"
    previous.save(previous_path)
    current.save(current_path)
    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_text(
        'version = 1\n[[questions]]\nid = "synthetic"\ntype = "single"\n'
        'frames = ["frame-000002-20260101T000002.000000Z.png"]\nseconds = [0.0]\n',
        encoding="utf-8",
    )

    result = generate_region_assets(manifest_path, write_crops=True)[0]

    assert result.previous_frame == previous_path.name
    assert result.region_grid
    assert result.crop_bounds is not None
    assert result.crop_path is not None
    assert Path(result.crop_path).is_file()


def test_generator_reports_missing_exact_previous_frame(tmp_path: Path) -> None:
    _, current = _synthetic_pair()
    current_path = tmp_path / "frame-000031-20260101T000100.000000Z.png"
    current.save(current_path)
    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_text(
        'version = 1\n[[questions]]\nid = "synthetic"\ntype = "single"\n'
        'frames = ["frame-000031-20260101T000100.000000Z.png"]\nseconds = [0.0]\n',
        encoding="utf-8",
    )

    result = generate_region_assets(manifest_path, write_crops=False)[0]

    assert result.previous_frame is None
    assert result.region_grid == ()
    assert result.reason == "原会话未落盘紧邻的前一帧"
