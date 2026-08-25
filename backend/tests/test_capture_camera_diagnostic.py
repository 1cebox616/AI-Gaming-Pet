"""Synthetic tests for the offline camera-motion diagnostic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
from PIL import Image

from pet.core.capture import FrameChangeDetector, PreparedFrame
from pet.core.capture_camera_diagnostic import (
    DiagnosticSelector,
    alignment_sensitivity,
    source_branch_lines,
    truth_mask,
)


def _prepared(value: int) -> PreparedFrame:
    image = Image.fromarray(
        np.full((180, 320, 3), value, dtype=np.uint8), mode="RGB"
    )
    return FrameChangeDetector().prepare(image)


def test_pre_gate_and_post_gate_ratios_are_distinct_for_one_poll_change() -> None:
    selector = DiagnosticSelector(
        persistence_polls=2,
        camera_motion_ratio=0.20,
        mouse_motion=(0.0, 2_000.0, 0.0),
        fixed_baselines={},
    )
    started = datetime(2026, 8, 25, tzinfo=timezone.utc)
    selector.observe(_prepared(0), sequence=1, timestamp=started)

    changed = selector.observe(
        _prepared(255),
        sequence=2,
        timestamp=started + timedelta(seconds=1),
    )

    assert changed.pre_gate_count == 16 * 9
    assert changed.pre_gate_ratio == 1.0
    assert changed.post_gate_count == 0
    assert changed.post_gate_ratio == 0.0
    assert changed.camera_motion is False
    assert changed.reason == "no_change"


def test_persistence_one_promotes_pre_gate_set_immediately() -> None:
    selector = DiagnosticSelector(
        persistence_polls=1,
        camera_motion_ratio=0.20,
        mouse_motion=(0.0, 2_000.0),
        fixed_baselines={},
    )
    started = datetime(2026, 8, 25, tzinfo=timezone.utc)
    selector.observe(_prepared(0), sequence=1, timestamp=started)

    changed = selector.observe(
        _prepared(255),
        sequence=2,
        timestamp=started + timedelta(seconds=1),
    )

    assert changed.pre_gate_ratio == changed.post_gate_ratio == 1.0
    assert changed.camera_motion is True
    assert changed.reason == "camera_motion"


def test_truth_mask_uses_inclusive_percentile_threshold() -> None:
    threshold, mask = truth_mask((0.0, 10.0, 20.0, 30.0), 0.75)

    assert threshold == 22.5
    assert mask == (False, False, False, True)


def test_camera_branch_precedes_persistent_branch_in_source() -> None:
    lines = source_branch_lines()

    assert lines.confirmed < lines.camera_if
    assert lines.camera_if < lines.persistent_elif


def test_alignment_sensitivity_moves_truth_to_neighboring_visual_frame() -> None:
    selector = DiagnosticSelector(
        persistence_polls=1,
        camera_motion_ratio=0.20,
        mouse_motion=(0.0, 2_000.0, 0.0),
        fixed_baselines={},
    )
    started = datetime(2026, 8, 25, tzinfo=timezone.utc)
    traces = (
        selector.observe(_prepared(0), sequence=1, timestamp=started),
        selector.observe(
            _prepared(0), sequence=2, timestamp=started + timedelta(seconds=1)
        ),
        selector.observe(
            _prepared(255), sequence=3, timestamp=started + timedelta(seconds=2)
        ),
    )

    rows = alignment_sensitivity(
        (False, True, False), traces, offsets=(0, 1)
    )

    assert rows[0].camera_hits == 0
    assert rows[0].pre_gate_median == 0.0
    assert rows[1].camera_hits == 1
    assert rows[1].pre_gate_median == 1.0
