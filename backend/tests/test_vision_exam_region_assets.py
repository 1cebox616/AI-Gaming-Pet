"""M5-T2.7 region generation uses only synthetic local images."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from pet.core.capture import FrameChangeDetector
from pet.games.generic.eval.region_assets import (
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


def test_generator_only_returns_grid_for_adjacent_frame(tmp_path: Path) -> None:
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

    files_before = set(tmp_path.iterdir())
    result = generate_region_assets(manifest_path)[0]

    assert result.previous_frame == previous_path.name
    assert result.region_grid
    assert set(tmp_path.iterdir()) == files_before


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

    result = generate_region_assets(manifest_path)[0]

    assert result.previous_frame is None
    assert result.region_grid == ()
    assert result.reason == "原会话未落盘紧邻的前一帧"
