"""M5-B-T2-2 calibration-rule regression tests."""

from __future__ import annotations

from pet.core.config import SceneConfig
from pet.games.generic.eval.scene_fingerprint_eval import (
    _bounded_stable_min_frames,
    _noise_floor_distances,
    _threshold_interval,
)


def test_scene_defaults_match_recalibrated_report() -> None:
    settings = SceneConfig()

    assert (
        settings.hash_kind,
        settings.hash_bits,
        settings.hamming_threshold,
        settings.stable_min_frames,
    ) == ("phash", 64, 8, 10)


def test_noise_floor_uses_only_no_change_frame_and_its_predecessor() -> None:
    hashes = (
        "0000000000000000",
        "0000000000000001",
        "ffffffffffffffff",
        "fffffffffffffffe",
    )
    reasons = ("initial", "no_change", "persistent_change", "no_change")

    assert _noise_floor_distances(hashes, reasons) == (1, 1)


def test_threshold_interval_eliminates_variant_when_noise_floor_exceeds_cap() -> None:
    lower, upper, reason = _threshold_interval((17, 17, 17), 64)

    assert (lower, upper) == (17, 16)
    assert reason == "噪声底 P95 下限 17 > 25% 上限 16"


def test_stable_min_frames_is_clamped_to_three_through_ten() -> None:
    same = (("0000000000000000",) * 5,)
    alternating = (
        tuple(
            "0000000000000000" if index % 2 == 0 else "ffffffffffffffff"
            for index in range(22)
        ),
    )

    assert _bounded_stable_min_frames(same, 0) == (1, 3)
    assert _bounded_stable_min_frames(alternating, 0) == (21, 10)
